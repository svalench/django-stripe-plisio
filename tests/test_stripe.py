from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.test import Client
from django.utils import timezone

from django_stripe_plisio.billing.enums import DiscountType, InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Price, PromoCode, UserEntitlement
from django_stripe_plisio.billing.services import create_invoice
from django_stripe_plisio.exceptions import PaymentProviderError
from django_stripe_plisio.payments.enums import (
    PaymentAttemptStatus,
    StripeSubscriptionStatus,
    WebhookProcessingStatus,
)
from django_stripe_plisio.payments.models import PaymentAttempt, StripeSubscription, WebhookEvent
from django_stripe_plisio.payments.services import create_checkout
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService
from django_stripe_plisio.signals import payment_mismatch

STRIPE = "django_stripe_plisio.payments.services.stripe_service.stripe"
SESSION_CREATE = f"{STRIPE}.checkout.Session.create"
SESSION_RETRIEVE = f"{STRIPE}.checkout.Session.retrieve"
COUPON_CREATE = f"{STRIPE}.Coupon.create"


def _session(session_id="cs_1"):
    return MagicMock(id=session_id, url=f"https://checkout.stripe.test/{session_id}")


def _paid_session(invoice, session_id="cs_1", amount=None, **extra):
    obj = {
        "id": session_id,
        "metadata": {"invoice_id": str(invoice.pk)},
        "client_reference_id": str(invoice.pk),
        "payment_status": "paid",
        "status": "complete",
        "amount_total": invoice.total_minor if amount is None else amount,
        "currency": invoice.currency.lower(),
    }
    obj.update(extra)
    return obj


@pytest.fixture
def monthly_price(product):
    return Price.objects.create(
        product=product,
        currency="USD",
        amount_minor=2000,
        billing_period="month",
    )


@pytest.mark.django_db
def test_stripe_price_id_uses_real_quantity(user, price):
    price.stripe_price_id = "price_123"
    price.save()
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, quantity=3)

    with patch(SESSION_CREATE, return_value=_session()) as mock_create:
        create_checkout(invoice)

    assert mock_create.call_args.kwargs["line_items"] == [{"price": "price_123", "quantity": 3}]


@pytest.mark.django_db
def test_discounted_one_time_charges_invoice_total(user, price):
    price.stripe_price_id = "price_123"
    price.save()
    PromoCode.objects.create(code="HALF", discount_type=DiscountType.PERCENT, percent_value=50)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="HALF")

    with patch(SESSION_CREATE, return_value=_session()) as mock_create:
        create_checkout(invoice)

    (item,) = mock_create.call_args.kwargs["line_items"]
    assert item["price_data"]["unit_amount"] == 500
    assert item["quantity"] == 1


@pytest.mark.django_db
def test_subscription_discount_is_one_time_coupon(user, monthly_price):
    PromoCode.objects.create(
        code="FIRST",
        discount_type=DiscountType.FIXED,
        fixed_amount_minor=500,
    )
    invoice = create_invoice(
        user,
        monthly_price,
        provider=PaymentProvider.STRIPE,
        promo_code="FIRST",
    )

    with (
        patch(COUPON_CREATE, return_value=MagicMock(id="coupon_1")) as mock_coupon,
        patch(SESSION_CREATE, return_value=_session()) as mock_create,
    ):
        create_checkout(invoice)

    params = mock_create.call_args.kwargs
    assert params["mode"] == "subscription"
    assert params["discounts"] == [{"coupon": "coupon_1"}]
    assert params["subscription_data"]["metadata"]["invoice_id"] == str(invoice.pk)
    assert params["line_items"][0]["price_data"]["unit_amount"] == 2000
    assert mock_coupon.call_args.kwargs["duration"] == "once"
    assert mock_coupon.call_args.kwargs["amount_off"] == 500


@pytest.mark.django_db
def test_long_checkout_url_is_stored(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    long_url = "https://checkout.stripe.com/c/pay/cs_test_" + "a" * 600 + "#fidkdWxOYHwnPyd1"
    session = MagicMock(id="cs_long", url=long_url)

    with patch(SESSION_CREATE, return_value=session):
        attempt = create_checkout(invoice)

    assert invoice.payment_url == long_url
    invoice.refresh_from_db()
    assert attempt.payment_url == long_url
    assert invoice.payment_url == long_url


@pytest.mark.django_db
def test_repeat_checkout_reuses_open_session(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    with patch(SESSION_CREATE, return_value=_session()) as mock_create:
        first = create_checkout(invoice)
        with patch(SESSION_RETRIEVE, return_value={"id": "cs_1", "status": "open"}):
            second = create_checkout(invoice)

    assert first.pk == second.pk
    assert mock_create.call_count == 1


@pytest.mark.django_db
def test_repeat_checkout_replaces_expired_session(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    with patch(SESSION_CREATE, side_effect=[_session("cs_old"), _session("cs_new")]):
        first = create_checkout(invoice)
        with patch(SESSION_RETRIEVE, return_value={"id": "cs_old", "status": "expired"}):
            second = create_checkout(invoice)

    first.refresh_from_db()
    invoice.refresh_from_db()
    assert first.status == PaymentAttemptStatus.CANCELLED
    assert second.external_id == "cs_new"
    assert invoice.external_id == "cs_new"


@pytest.mark.django_db
def test_stripe_error_persists_failed_attempt(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    with patch(SESSION_CREATE, side_effect=stripe.APIConnectionError("down")):
        with pytest.raises(PaymentProviderError) as exc_info:
            create_checkout(invoice)

    attempt = exc_info.value.attempt
    assert attempt is not None
    assert PaymentAttempt.objects.get(pk=attempt.pk).status == PaymentAttemptStatus.FAILED


@pytest.mark.django_db
def test_free_invoice_paid_without_provider(user, price):
    PromoCode.objects.create(code="FREE", discount_type=DiscountType.PERCENT, percent_value=100)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="FREE")

    with patch(SESSION_CREATE) as mock_create:
        attempt = create_checkout(invoice)

    mock_create.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID
    assert attempt.status == PaymentAttemptStatus.SUCCEEDED


@pytest.mark.django_db
def test_underpaid_session_not_applied(user, price, django_capture_on_commit_callbacks):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    reasons = []

    def on_mismatch(sender, reason, **kwargs):
        reasons.append(reason)

    payment_mismatch.connect(on_mismatch)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            applied = StripePaymentService()._apply_checkout_session_paid(
                _paid_session(invoice, amount=1),
            )
    finally:
        payment_mismatch.disconnect(on_mismatch)

    invoice.refresh_from_db()
    assert applied is False
    assert invoice.status == InvoiceStatus.PENDING
    assert reasons == ["amount_mismatch"]


@pytest.mark.django_db
def test_async_payment_waits_for_success_event(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    service = StripePaymentService()
    unpaid = _paid_session(invoice, payment_status="unpaid")

    service.handle_webhook_event(
        {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": unpaid}},
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PENDING

    service.handle_webhook_event(
        {
            "id": "evt_2",
            "type": "checkout.session.async_payment_succeeded",
            "data": {"object": _paid_session(invoice)},
        },
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID


@pytest.mark.django_db
def test_webhook_view_returns_500_and_persists_failure(user, price, settings):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    event = {
        "id": "evt_boom",
        "type": "checkout.session.completed",
        "data": {"object": _paid_session(invoice)},
    }
    with (
        patch.object(StripePaymentService, "verify_webhook", return_value=event),
        patch(
            "django_stripe_plisio.payments.services.stripe_service.mark_invoice_paid",
            side_effect=RuntimeError("db down"),
        ),
    ):
        response = Client().post(
            "/billing/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
        )

    assert response.status_code == 500
    webhook = WebhookEvent.objects.get(idempotency_key="stripe:evt_boom")
    assert webhook.status == WebhookProcessingStatus.FAILED


@pytest.mark.django_db
def test_subscription_lifecycle_updates_entitlement(user, monthly_price):
    invoice = create_invoice(user, monthly_price, provider=PaymentProvider.STRIPE)
    service = StripePaymentService()
    period_end = int((timezone.now() + timedelta(days=30)).timestamp())
    sub_obj = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "metadata": {"invoice_id": str(invoice.pk)},
        # API 2025-03-31+: период в items.data[]
        "items": {"data": [{"current_period_end": period_end}]},
    }

    # subscription.created может прийти раньше checkout.session.completed
    service.handle_webhook_event(
        {"id": "evt_s1", "type": "customer.subscription.created", "data": {"object": sub_obj}},
    )
    service.handle_webhook_event(
        {
            "id": "evt_c1",
            "type": "checkout.session.completed",
            "data": {"object": _paid_session(invoice, subscription="sub_1", customer="cus_1")},
        },
    )

    subscription = StripeSubscription.objects.get(stripe_subscription_id="sub_1")
    entitlement = UserEntitlement.objects.get(invoice=invoice)
    assert subscription.status == StripeSubscriptionStatus.ACTIVE
    assert subscription.entitlement_id == entitlement.pk
    assert subscription.current_period_end is not None
    assert int(entitlement.active_until.timestamp()) == period_end

    renewed_end = period_end + 30 * 24 * 3600
    service.handle_webhook_event(
        {
            "id": "evt_s2",
            "type": "customer.subscription.updated",
            "data": {"object": {**sub_obj, "metadata": {}, "current_period_end": renewed_end}},
        },
    )
    entitlement.refresh_from_db()
    assert int(entitlement.active_until.timestamp()) == renewed_end

    service.handle_webhook_event(
        {"id": "evt_s3", "type": "customer.subscription.deleted", "data": {"object": sub_obj}},
    )
    subscription.refresh_from_db()
    entitlement.refresh_from_db()
    assert subscription.status == StripeSubscriptionStatus.CANCELED
    assert entitlement.active_until <= timezone.now()
    assert entitlement.is_active is False

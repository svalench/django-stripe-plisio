"""Воспроизведения дефектов платёжной корректности.

Ожидания в assert — безопасное поведение. Тесты падают на текущем коде
и не ходят в Stripe/Plisio (HTTP замокан).
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.utils import timezone

from django_stripe_plisio.billing.enums import (
    BillingPeriod,
    DiscountType,
    InvoiceStatus,
    PaymentProvider,
)
from django_stripe_plisio.billing.models import Price, PromoCode, UserEntitlement
from django_stripe_plisio.billing.money import minor_to_major_amount
from django_stripe_plisio.billing.services import (
    create_invoice,
    expire_pending_invoices,
    get_user_balance,
    mark_invoice_paid,
)
from django_stripe_plisio.exceptions import BillingError, PaymentProviderError
from django_stripe_plisio.payments.enums import PaymentAttemptStatus
from django_stripe_plisio.payments.models import PaymentAttempt
from django_stripe_plisio.payments.services import create_checkout
from django_stripe_plisio.payments.services.invoice_sync import sync_pending_invoices
from django_stripe_plisio.payments.services.plisio_service import PlisioPaymentService
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService
from django_stripe_plisio.signals import payment_mismatch

PLISIO_GET = "django_stripe_plisio.payments.services.plisio_service.requests.get"


def _plisio_ok(txn_id="txn_old", source_amount="10.00"):
    return MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "status": "success",
                "data": {
                    "txn_id": txn_id,
                    "invoice_url": "https://plisio.test/invoice/txn_old",
                    "source_amount": source_amount,
                    "source_currency": "USD",
                },
            },
        ),
    )


@pytest.mark.django_db
def test_plisio_completed_rejects_lower_source_amount(user, price):
    """completed с суммой меньше счёта не должен зачислять полный total_minor."""
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    assert invoice.total_minor == 1000
    assert minor_to_major_amount(invoice.total_minor, "USD") == "10.00"

    PlisioPaymentService().handle_webhook_event(
        {
            "txn_id": "txn_low",
            "status": "completed",
            "order_number": str(invoice.pk),
            "source_currency": "USD",
            "source_amount": "0.01",
            "amount": "0.0001",
        },
    )

    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PENDING
    assert get_user_balance(user, "USD") == 0
    assert UserEntitlement.objects.filter(invoice=invoice).count() == 0


@pytest.mark.django_db
def test_stale_plisio_invoice_not_reused_after_total_grows(user, price):
    """Таймаут после создания инвойса + смена промокода не должна оставлять старую сумму."""
    PromoCode.objects.create(code="OFF90", discount_type=DiscountType.PERCENT, percent_value=90)
    PromoCode.objects.create(code="OFF10", discount_type=DiscountType.PERCENT, percent_value=10)
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO, promo_code="OFF90")
    assert invoice.total_minor == 100

    with patch(PLISIO_GET, side_effect=requests.Timeout("read timed out")):
        with pytest.raises(PaymentProviderError):
            create_checkout(invoice)

    invoice.refresh_from_db()
    assert invoice.external_id == ""
    from django_stripe_plisio.billing.services import apply_promo

    apply_promo(invoice, "OFF10")
    invoice.refresh_from_db()
    assert invoice.total_minor == 900

    with patch(PLISIO_GET, return_value=_plisio_ok(source_amount="1.00")) as mocked:
        with pytest.raises(PaymentProviderError):
            create_checkout(invoice)

    sent = mocked.call_args.kwargs["params"]["source_amount"]
    assert sent == "9.00"
    attempt = PaymentAttempt.objects.filter(invoice=invoice).order_by("-pk").first()
    assert attempt.status == PaymentAttemptStatus.FAILED


@pytest.mark.django_db
def test_stripe_overpayment_is_not_silently_accepted(
    user,
    price,
    django_capture_on_commit_callbacks,
):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    reasons = []

    def on_mismatch(sender, reason, **kwargs):
        reasons.append(reason)

    payment_mismatch.connect(on_mismatch)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            applied = StripePaymentService()._apply_checkout_session_paid(
                {
                    "id": "cs_over",
                    "metadata": {"invoice_id": str(invoice.pk)},
                    "payment_status": "paid",
                    "amount_total": invoice.total_minor + 5000,
                    "currency": "usd",
                },
            )
    finally:
        payment_mismatch.disconnect(on_mismatch)

    invoice.refresh_from_db()
    assert applied is False
    assert invoice.status == InvoiceStatus.PENDING
    assert reasons == ["amount_mismatch"]


@pytest.mark.django_db
def test_sync_plisio_mismatch_emits_signal(
    user,
    price,
    settings,
    django_capture_on_commit_callbacks,
):
    settings.DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "k"
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "* * * * *"},
    }
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    invoice.external_id = "txn_sync"
    invoice.save(update_fields=["external_id"])
    reasons = []

    def on_mismatch(sender, reason, **kwargs):
        reasons.append(reason)

    payment_mismatch.connect(on_mismatch)
    operation = {
        "status": "success",
        "data": {
            "status": "mismatch",
            "order_number": str(invoice.pk),
            "txn_id": "txn_sync",
            "source_amount": "15.00",
            "source_currency": "USD",
            "amount": "0.002",
        },
    }
    try:
        with (
            django_capture_on_commit_callbacks(execute=True),
            patch(
                PLISIO_GET,
                return_value=MagicMock(json=MagicMock(return_value=operation)),
            ),
        ):
            result = sync_pending_invoices()
    finally:
        payment_mismatch.disconnect(on_mismatch)

    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PENDING
    assert result.skipped == 1
    assert reasons == ["mismatch"]


@pytest.mark.django_db
def test_sync_plisio_expired_partial_is_not_plain_expiry(
    user,
    price,
    settings,
    django_capture_on_commit_callbacks,
):
    settings.DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "k"
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "* * * * *"},
    }
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    invoice.external_id = "txn_exp"
    invoice.save(update_fields=["external_id"])
    reasons = []

    def on_mismatch(sender, reason, **kwargs):
        reasons.append(reason)

    payment_mismatch.connect(on_mismatch)
    operation = {
        "status": "success",
        "data": {
            "status": "expired",
            "order_number": str(invoice.pk),
            "amount": "0.004",
            "source_currency": "USD",
            "source_amount": "10.00",
        },
    }
    try:
        with (
            django_capture_on_commit_callbacks(execute=True),
            patch(PLISIO_GET, return_value=MagicMock(json=MagicMock(return_value=operation))),
        ):
            sync_pending_invoices()
    finally:
        payment_mismatch.disconnect(on_mismatch)

    invoice.refresh_from_db()
    # Частичная оплата не должна выглядеть как обычное истечение без алерта.
    assert reasons == ["expired_partial"]
    assert invoice.status == InvoiceStatus.PENDING


@pytest.mark.django_db
def test_plisio_error_marks_invoice_failed(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    PlisioPaymentService().handle_webhook_event(
        {"txn_id": "txn_err", "status": "error", "order_number": str(invoice.pk)},
    )
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.FAILED


@pytest.mark.django_db
def test_local_ttl_keeps_checked_out_invoice_until_sync(user, price, settings):
    """Счёт с external_id не истекает локально: sync ещё может увидеть completed."""
    settings.DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "k"
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "* * * * *"},
    }
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    invoice.external_id = "txn_late"
    invoice.expires_at = timezone.now() - timedelta(minutes=5)
    invoice.save(update_fields=["external_id", "expires_at"])
    assert expire_pending_invoices() == 0
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PENDING

    operation = {
        "status": "success",
        "data": {
            "status": "completed",
            "order_number": str(invoice.pk),
            "txn_id": "txn_late",
            "source_amount": "10.00",
            "source_currency": "USD",
        },
    }
    with patch(PLISIO_GET, return_value=MagicMock(json=MagicMock(return_value=operation))):
        result = sync_pending_invoices()

    invoice.refresh_from_db()
    assert result.paid == 1
    assert invoice.status == InvoiceStatus.PAID
    assert get_user_balance(user, "USD") == 1000


@pytest.mark.django_db
def test_recurring_price_does_not_grant_perpetual_entitlement(user, product):
    monthly = Price.objects.create(
        product=product,
        currency="USD",
        amount_minor=2000,
        billing_period=BillingPeriod.MONTH,
    )
    invoice = create_invoice(user, monthly, provider=PaymentProvider.STRIPE)
    mark_invoice_paid(invoice)
    entitlement = UserEntitlement.objects.get(invoice=invoice)
    assert entitlement.active_until is not None
    assert entitlement.active_until <= timezone.now() + timedelta(days=32)


@pytest.mark.django_db
def test_plisio_rejects_recurring_price(user, product):
    monthly = Price.objects.create(
        product=product,
        currency="USD",
        amount_minor=2000,
        billing_period=BillingPeriod.MONTH,
    )
    with pytest.raises(BillingError):
        create_invoice(user, monthly, provider=PaymentProvider.PLISIO)


@pytest.mark.django_db
def test_ugx_stripe_amount_follows_stripe_special_case(user, product, settings):
    """5 UGX у Stripe — amount 500 (двухзнаковая совместимость), не 5."""
    settings.DJANGO_STRIPE_PLISIO_ALLOWED_CURRENCIES = ["USD", "UGX"]
    price = Price.objects.create(
        product=product,
        currency="UGX",
        amount_minor=5,
        billing_period=BillingPeriod.ONE_TIME,
    )
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    line = invoice.lines.select_related("price").first()
    items = StripePaymentService()._build_line_items(invoice, line, "payment")
    assert items[0]["price_data"]["unit_amount"] == 500


@pytest.mark.django_db
def test_fixed_discount_without_currency_does_not_cross_currencies(user, price):
    PromoCode.objects.create(
        code="FIVE",
        discount_type=DiscountType.FIXED,
        fixed_amount_minor=500,
        fixed_currency="",
    )
    with pytest.raises(BillingError):
        create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="FIVE")

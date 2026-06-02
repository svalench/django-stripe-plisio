from unittest.mock import MagicMock, patch

import pytest

from django_stripe_plisio.billing.enums import InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.services import create_invoice, mark_invoice_paid
from django_stripe_plisio.payments.enums import PaymentAttemptStatus
from django_stripe_plisio.payments.models import PaymentAttempt
from django_stripe_plisio.payments.services import create_checkout
from django_stripe_plisio.payments.services.invoice_sync import sync_pending_invoices
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService


@pytest.fixture
def stripe_pending_invoice(user, price, settings):
    settings.DJANGO_STRIPE_PLISIO_STRIPE_SECRET_KEY = "sk_test_x"
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "*/10 * * * *"},
    }
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    invoice.external_id = "cs_sync_test"
    invoice.payment_url = "https://checkout.stripe.test/session"
    invoice.save(update_fields=["external_id", "payment_url", "updated_at"])
    PaymentAttempt.objects.create(
        invoice=invoice,
        provider=PaymentProvider.STRIPE,
        status=PaymentAttemptStatus.PENDING,
        external_id="cs_sync_test",
        payment_url=invoice.payment_url,
    )
    return invoice


@pytest.fixture
def plisio_pending_invoice(user, price, settings):
    settings.DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "plisio_test_key"
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "*/10 * * * *"},
    }
    invoice = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    plisio_response = {
        "status": "success",
        "data": {
            "txn_id": "txn_sync_test",
            "invoice_url": "https://plisio.test/inv",
        },
    }
    with patch(
        "django_stripe_plisio.payments.services.plisio_service.requests.get",
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=plisio_response),
        ),
    ):
        create_checkout(invoice)
    return invoice


@pytest.mark.django_db
def test_sync_stripe_paid(stripe_pending_invoice):
    session = {
        "id": "cs_sync_test",
        "payment_status": "paid",
        "status": "complete",
        "metadata": {"invoice_id": str(stripe_pending_invoice.pk)},
        "client_reference_id": str(stripe_pending_invoice.pk),
    }
    with patch(
        "django_stripe_plisio.payments.services.stripe_service.stripe.checkout.Session.retrieve",
        return_value=session,
    ):
        result = sync_pending_invoices()

    stripe_pending_invoice.refresh_from_db()
    assert stripe_pending_invoice.status == InvoiceStatus.PAID
    assert result.paid == 1
    assert result.checked == 1


@pytest.mark.django_db
def test_sync_stripe_idempotent(stripe_pending_invoice):
    mark_invoice_paid(stripe_pending_invoice, external_id="cs_sync_test")

    with patch(
        "django_stripe_plisio.payments.services.stripe_service.stripe.checkout.Session.retrieve",
    ) as mock_retrieve:
        result = sync_pending_invoices()
        mock_retrieve.assert_not_called()

    assert result.checked == 0
    assert result.paid == 0


@pytest.mark.django_db
def test_sync_plisio_completed(plisio_pending_invoice):
    operation_response = {
        "status": "success",
        "data": {
            "status": "completed",
            "order_number": str(plisio_pending_invoice.pk),
            "txn_id": "txn_sync_test",
        },
    }

    def mock_get(url, **kwargs):
        if "/operations/" in url:
            return MagicMock(
                raise_for_status=MagicMock(),
                json=MagicMock(return_value=operation_response),
            )
        raise AssertionError(f"unexpected url: {url}")

    with patch(
        "django_stripe_plisio.payments.services.plisio_service.requests.get",
        side_effect=mock_get,
    ):
        result = sync_pending_invoices()

    plisio_pending_invoice.refresh_from_db()
    assert plisio_pending_invoice.status == InvoiceStatus.PAID
    assert result.paid == 1


@pytest.mark.django_db
def test_sync_plisio_mismatch_skipped(plisio_pending_invoice):
    operation_response = {
        "status": "success",
        "data": {
            "status": "mismatch",
            "order_number": str(plisio_pending_invoice.pk),
            "txn_id": "txn_sync_test",
        },
    }

    with patch(
        "django_stripe_plisio.payments.services.plisio_service.requests.get",
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=operation_response),
        ),
    ):
        result = sync_pending_invoices()

    plisio_pending_invoice.refresh_from_db()
    assert plisio_pending_invoice.status == InvoiceStatus.PENDING
    assert result.skipped == 1


@pytest.mark.django_db
def test_sync_plisio_expired(plisio_pending_invoice):
    operation_response = {
        "status": "success",
        "data": {"status": "expired", "txn_id": "txn_sync_test"},
    }

    with patch(
        "django_stripe_plisio.payments.services.plisio_service.requests.get",
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=operation_response),
        ),
    ):
        result = sync_pending_invoices()

    plisio_pending_invoice.refresh_from_db()
    assert plisio_pending_invoice.status == InvoiceStatus.EXPIRED
    assert result.expired_remote == 1

    attempt = PaymentAttempt.objects.filter(invoice=plisio_pending_invoice).first()
    assert attempt.status == PaymentAttemptStatus.FAILED


@pytest.mark.django_db
def test_dsp_sync_invoices_disabled(settings):
    from django.core.management import call_command

    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": False, "schedule": "*/10 * * * *"},
    }
    call_command("dsp_sync_invoices")


@pytest.mark.django_db
def test_stripe_apply_checkout_session_paid_refactor(stripe_pending_invoice):
    service = StripePaymentService()
    obj = {
        "id": "cs_sync_test",
        "metadata": {"invoice_id": str(stripe_pending_invoice.pk)},
        "client_reference_id": str(stripe_pending_invoice.pk),
    }
    assert service._apply_checkout_session_paid(obj) is True
    stripe_pending_invoice.refresh_from_db()
    assert stripe_pending_invoice.status == InvoiceStatus.PAID

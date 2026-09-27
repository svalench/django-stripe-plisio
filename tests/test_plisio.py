import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.test import Client

from django_stripe_plisio.billing.enums import InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.services import create_invoice
from django_stripe_plisio.exceptions import PaymentProviderError, WebhookVerificationError
from django_stripe_plisio.payments.enums import PaymentAttemptStatus, WebhookProcessingStatus
from django_stripe_plisio.payments.models import PaymentAttempt, WebhookEvent
from django_stripe_plisio.payments.services import create_checkout
from django_stripe_plisio.payments.services.plisio_service import (
    PlisioPaymentService,
    json_signature_base,
    php_signature_base,
)
from django_stripe_plisio.signals import payment_mismatch

SECRET = "plisio_test_secret"
PLISIO_GET = "django_stripe_plisio.payments.services.plisio_service.requests.get"


def _sign(base: str) -> str:
    return hmac.new(SECRET.encode(), base.encode(), hashlib.sha1).hexdigest()


def _plisio_ok(txn_id="txn_1", url="https://plisio.test/invoice/txn_1"):
    return MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={"status": "success", "data": {"txn_id": txn_id, "invoice_url": url}},
        ),
    )


@pytest.fixture
def plisio_invoice(user, price):
    return create_invoice(user, price, provider=PaymentProvider.PLISIO)


def test_php_signature_base_matches_php_serialize():
    # PHP: serialize(["a" => "ü", "b" => "2"]) после ksort; длина — в байтах UTF-8
    assert php_signature_base({"b": "2", "a": "ü"}) == 'a:2:{s:1:"a";s:2:"ü";s:1:"b";s:1:"2";}'


def test_php_signature_base_decodes_tx_urls():
    assert php_signature_base({"tx_urls": "a&amp;b"}) == 'a:1:{s:7:"tx_urls";s:3:"a&b";}'


def test_json_signature_base_keeps_order_and_is_compact():
    payload = {"txn_id": "t", "amount": "0.1", "confirmations": 2, "rate": 5.0}
    assert (
        json_signature_base(payload) == '{"txn_id":"t","amount":"0.1","confirmations":2,"rate":5}'
    )


@pytest.mark.django_db
def test_json_callback_valid_signature_marks_paid(plisio_invoice):
    data = {
        "txn_id": "txn_json",
        "status": "completed",
        "order_number": str(plisio_invoice.pk),
        "source_currency": "USD",
    }
    data["verify_hash"] = _sign(json_signature_base(data))

    response = Client().post(
        "/billing/webhooks/plisio/?json=true",
        data=json.dumps(data),
        content_type="application/json",
    )

    assert response.status_code == 200
    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.PAID


@pytest.mark.django_db
def test_form_callback_valid_signature_marks_paid(plisio_invoice):
    data = {"txn_id": "txn_form", "status": "completed", "order_number": str(plisio_invoice.pk)}
    data["verify_hash"] = _sign(php_signature_base(data))

    response = Client().post("/billing/webhooks/plisio/", data=data)

    assert response.status_code == 200
    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.PAID


@pytest.mark.django_db
def test_callback_invalid_signature_rejected(plisio_invoice):
    data = {
        "txn_id": "txn_bad",
        "status": "completed",
        "order_number": str(plisio_invoice.pk),
        "verify_hash": "0" * 40,
    }
    response = Client().post(
        "/billing/webhooks/plisio/",
        data=json.dumps(data),
        content_type="application/json",
    )

    assert response.status_code == 400
    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.PENDING


@pytest.mark.django_db
def test_missing_hash_rejected_even_if_not_required(settings):
    settings.DJANGO_STRIPE_PLISIO_REQUIRE_WEBHOOK_SECRET = False
    with pytest.raises(WebhookVerificationError, match="Missing"):
        PlisioPaymentService()._verify_plisio_data({"status": "completed"})


@pytest.mark.django_db
def test_callback_currency_mismatch_not_paid(plisio_invoice, django_capture_on_commit_callbacks):
    received = []

    def on_mismatch(sender, reason, **kwargs):
        received.append(reason)

    payment_mismatch.connect(on_mismatch)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            PlisioPaymentService().handle_webhook_event(
                {
                    "txn_id": "txn_eur",
                    "status": "completed",
                    "order_number": str(plisio_invoice.pk),
                    "source_currency": "EUR",
                },
            )
    finally:
        payment_mismatch.disconnect(on_mismatch)

    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.PENDING
    assert received == ["currency_mismatch"]


@pytest.mark.django_db
def test_callback_expired_updates_invoice(plisio_invoice):
    PlisioPaymentService().handle_webhook_event(
        {"txn_id": "txn_exp", "status": "expired", "order_number": str(plisio_invoice.pk)},
    )
    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.EXPIRED


@pytest.mark.django_db
def test_checkout_adds_json_flag_and_redirect_urls(plisio_invoice, settings):
    settings.DJANGO_STRIPE_PLISIO_SUCCESS_URL = "https://shop.test/ok"
    with patch(PLISIO_GET, return_value=_plisio_ok()) as mock_get:
        attempt = create_checkout(plisio_invoice)

    params = mock_get.call_args.kwargs["params"]
    assert params["callback_url"].endswith("?json=true")
    assert params["success_invoice_url"] == "https://shop.test/ok"
    assert "success_callback_url" not in params
    assert attempt.status == PaymentAttemptStatus.PENDING
    assert "api_key" not in attempt.request_payload


@pytest.mark.django_db
def test_repeat_checkout_reuses_invoice(plisio_invoice):
    with patch(PLISIO_GET, return_value=_plisio_ok()) as mock_get:
        first = create_checkout(plisio_invoice)
        second = create_checkout(plisio_invoice)

    assert first.pk == second.pk
    assert mock_get.call_count == 1


@pytest.mark.django_db
def test_api_key_not_leaked_on_http_error(plisio_invoice, settings):
    settings.DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "super_secret_key"
    error = requests.HTTPError(
        "500 Server Error for url: https://api.plisio.net/api/v1/invoices/new?api_key=super_secret_key",
    )
    with patch(PLISIO_GET, side_effect=error):
        with pytest.raises(PaymentProviderError) as exc_info:
            create_checkout(plisio_invoice)

    attempt = PaymentAttempt.objects.get(invoice=plisio_invoice)
    assert attempt.status == PaymentAttemptStatus.FAILED
    assert "super_secret_key" not in attempt.error_message
    assert "super_secret_key" not in str(exc_info.value)


@pytest.mark.django_db
def test_sync_rejects_foreign_order_number(plisio_invoice):
    from django_stripe_plisio.payments.services.invoice_sync import sync_pending_invoices

    with patch(PLISIO_GET, return_value=_plisio_ok()):
        create_checkout(plisio_invoice)

    foreign = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "status": "success",
                "data": {"status": "completed", "order_number": "999999", "txn_id": "txn_1"},
            },
        ),
    )
    with patch(PLISIO_GET, return_value=foreign):
        result = sync_pending_invoices()

    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.PENDING
    assert result.errors == 1


@pytest.mark.django_db
def test_webhook_failure_is_persisted(plisio_invoice):
    service = PlisioPaymentService()
    event = {"txn_id": "txn_fail", "status": "completed", "order_number": str(plisio_invoice.pk)}

    with patch(
        "django_stripe_plisio.payments.services.plisio_service.mark_invoice_paid",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            service.handle_webhook_event(event)

    webhook = WebhookEvent.objects.get(idempotency_key="plisio:txn_fail:completed")
    assert webhook.status == WebhookProcessingStatus.FAILED
    assert webhook.error_message == "boom"
    plisio_invoice.refresh_from_db()
    assert plisio_invoice.status == InvoiceStatus.PENDING

    # Повторная доставка после исправления обрабатывается
    service.handle_webhook_event(event)
    webhook.refresh_from_db()
    assert webhook.status == WebhookProcessingStatus.PROCESSED

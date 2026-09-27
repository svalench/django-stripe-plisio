import hashlib
import hmac
import json
import time

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from django_stripe_plisio.billing.enums import InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Price, Product
from django_stripe_plisio.billing.services import create_invoice
from django_stripe_plisio.exceptions import WebhookVerificationError
from django_stripe_plisio.payments.services.plisio_service import PlisioPaymentService
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService


@pytest.fixture
def pending_invoice(db):
    user = get_user_model().objects.create_user(username="wh", password="x")
    product = Product.objects.create(code="x", name="X")
    price = Price.objects.create(product=product, currency="USD", amount_minor=100)
    return create_invoice(user, price, provider=PaymentProvider.STRIPE)


@pytest.mark.django_db
@override_settings(DJANGO_STRIPE_PLISIO_STRIPE_WEBHOOK_SECRET="")
def test_stripe_webhook_rejects_without_secret(pending_invoice):
    client = Client()
    payload = json.dumps({"id": "evt_x", "type": "checkout.session.completed"}).encode()
    response = client.post(
        "/billing/webhooks/stripe/",
        data=payload,
        content_type="application/json",
    )
    assert response.status_code == 400
    pending_invoice.refresh_from_db()
    assert pending_invoice.status != InvoiceStatus.PAID


def _stripe_signature(payload: str, secret: str, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


@pytest.mark.django_db
def test_stripe_webhook_real_signature_marks_paid(pending_invoice):
    event = {
        "id": "evt_signed",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_signed",
                "metadata": {"invoice_id": str(pending_invoice.pk)},
                "payment_status": "paid",
                "amount_total": pending_invoice.total_minor,
                "currency": "usd",
            },
        },
    }
    payload = json.dumps(event)
    response = Client().post(
        "/billing/webhooks/stripe/",
        data=payload,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=_stripe_signature(payload, "whsec_test_secret", int(time.time())),
    )

    assert response.status_code == 200
    pending_invoice.refresh_from_db()
    assert pending_invoice.status == InvoiceStatus.PAID


@pytest.mark.django_db
def test_stripe_webhook_stale_signature_rejected(pending_invoice):
    payload = json.dumps({"id": "evt_old", "type": "checkout.session.completed"})
    stale = int(time.time()) - 3600
    response = Client().post(
        "/billing/webhooks/stripe/",
        data=payload,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=_stripe_signature(payload, "whsec_test_secret", stale),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_stripe_verify_requires_secret():
    service = StripePaymentService()
    with override_settings(DJANGO_STRIPE_PLISIO_STRIPE_WEBHOOK_SECRET=""):
        with pytest.raises(WebhookVerificationError):
            service.verify_webhook(b"{}", {})


@pytest.mark.django_db
@override_settings(DJANGO_STRIPE_PLISIO_PLISIO_CALLBACK_SECRET="")
def test_plisio_verify_rejects_without_secret():
    service = PlisioPaymentService()
    with pytest.raises(WebhookVerificationError):
        service._verify_plisio_data({"status": "completed", "verify_hash": "abc"})

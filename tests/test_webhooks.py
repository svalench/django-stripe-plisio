from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from django_stripe_plisio.billing.enums import InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Price, Product
from django_stripe_plisio.billing.services import create_invoice
from django_stripe_plisio.payments.enums import WebhookProcessingStatus
from django_stripe_plisio.payments.models import WebhookEvent
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService


@pytest.fixture
def paid_invoice(db):
    User = get_user_model()
    user = User.objects.create_user(username="wh", password="x")
    product = Product.objects.create(code="x", name="X")
    price = Price.objects.create(product=product, currency="USD", amount_minor=500)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    return invoice


@pytest.mark.django_db
def test_stripe_webhook_idempotent(paid_invoice):
    service = StripePaymentService()
    event = {
        "id": "evt_test_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test",
                "metadata": {"invoice_id": str(paid_invoice.pk)},
                "client_reference_id": str(paid_invoice.pk),
            },
        },
    }

    with patch.object(StripePaymentService, "verify_webhook", return_value=event):
        service.handle_webhook_event(event)
        service.handle_webhook_event(event)

    paid_invoice.refresh_from_db()
    assert paid_invoice.status == InvoiceStatus.PAID
    assert WebhookEvent.objects.filter(idempotency_key="stripe:evt_test_1").count() == 1
    wh = WebhookEvent.objects.get(idempotency_key="stripe:evt_test_1")
    assert wh.status == WebhookProcessingStatus.PROCESSED

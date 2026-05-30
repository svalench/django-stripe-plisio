from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.billing.models import BalanceLedger, Price, Product
from django_stripe_plisio.billing.services import create_invoice, get_user_balance
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService


@pytest.mark.django_db
def test_double_webhook_single_ledger():
    user = get_user_model().objects.create_user(username="race", password="x")
    product = Product.objects.create(code="r", name="R")
    price = Price.objects.create(product=product, currency="USD", amount_minor=500)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)

    event = {
        "id": "evt_race_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_race",
                "metadata": {"invoice_id": str(invoice.pk)},
                "client_reference_id": str(invoice.pk),
            },
        },
    }

    service = StripePaymentService()
    with patch.object(service, "verify_webhook", return_value=event):
        service.handle_webhook_event(event)
        service.handle_webhook_event(event)

    assert BalanceLedger.objects.filter(reference=f"invoice:{invoice.pk}").count() == 1
    assert get_user_balance(user, "USD") == 500

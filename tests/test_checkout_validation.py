import pytest

from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.billing.models import Price, Product
from django_stripe_plisio.billing.services import create_invoice, mark_invoice_paid
from django_stripe_plisio.payments.services import create_checkout, validate_invoice_for_checkout


@pytest.mark.django_db
def test_cannot_checkout_paid_invoice(user):
    product = Product.objects.create(code="c", name="C")
    price = Price.objects.create(product=product, currency="USD", amount_minor=100)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    invoice = mark_invoice_paid(invoice)

    with pytest.raises(ValueError, match="not payable"):
        validate_invoice_for_checkout(invoice)

    with pytest.raises(ValueError, match="not payable"):
        create_checkout(invoice)


@pytest.mark.django_db
def test_inactive_price_rejected(user):
    product = Product.objects.create(code="i", name="I", is_active=True)
    price = Price.objects.create(product=product, currency="USD", amount_minor=100, is_active=False)

    with pytest.raises(ValueError, match="not active"):
        create_invoice(user, price, provider=PaymentProvider.STRIPE)

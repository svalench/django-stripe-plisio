import pytest

from django_stripe_plisio.billing.enums import DiscountType, PaymentProvider
from django_stripe_plisio.billing.models import Price, Product, PromoCode
from django_stripe_plisio.billing.services import create_invoice, mark_invoice_paid


@pytest.fixture
def price(db):
    product = Product.objects.create(code="p", name="P")
    return Price.objects.create(product=product, currency="USD", amount_minor=1000)


@pytest.mark.django_db
def test_promo_used_count_on_paid_only(user, price):
    promo = PromoCode.objects.create(
        code="ONPAID",
        discount_type=DiscountType.PERCENT,
        percent_value=10,
        is_active=True,
    )
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="ONPAID")
    promo.refresh_from_db()
    assert promo.used_count == 0

    mark_invoice_paid(invoice)
    promo.refresh_from_db()
    assert promo.used_count == 1

import pytest

from django_stripe_plisio.billing.enums import (
    DiscountType,
    InvoiceStatus,
    LedgerEntryType,
    PaymentProvider,
)
from django_stripe_plisio.billing.models import Price, Product, PromoCode
from django_stripe_plisio.billing.services import (
    calculate_discount_minor,
    create_invoice,
    get_user_balance,
    grant_private_discount,
    mark_invoice_paid,
    record_ledger_entry,
)


@pytest.fixture
def product(db):
    return Product.objects.create(code="pro", name="Pro Plan")


@pytest.fixture
def price(product):
    return Price.objects.create(
        product=product,
        currency="USD",
        amount_minor=1000,
        billing_period="one_time",
    )


@pytest.mark.django_db
def test_calculate_percent_discount():
    assert calculate_discount_minor(1000, "USD", DiscountType.PERCENT, percent_value=10) == 100


@pytest.mark.django_db
def test_calculate_fixed_discount_wrong_currency():
    assert (
        calculate_discount_minor(
            1000,
            "USD",
            DiscountType.FIXED,
            fixed_amount_minor=500,
            fixed_currency="EUR",
        )
        == 0
    )


@pytest.mark.django_db
def test_create_invoice(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, quantity=2)
    assert invoice.subtotal_minor == 2000
    assert invoice.total_minor == 2000
    assert invoice.status == InvoiceStatus.PENDING
    assert invoice.lines.count() == 1


@pytest.mark.django_db
def test_create_invoice_with_promo(user, price):
    PromoCode.objects.create(
        code="SAVE20",
        discount_type=DiscountType.PERCENT,
        percent_value=20,
        is_active=True,
    )
    invoice = create_invoice(
        user,
        price,
        provider=PaymentProvider.PLISIO,
        promo_code="SAVE20",
    )
    assert invoice.discount_minor == 200
    assert invoice.total_minor == 800


@pytest.mark.django_db
def test_private_grant_discount(user, price):
    grant_private_discount(
        user,
        discount_type=DiscountType.FIXED,
        fixed_amount_minor=300,
        fixed_currency="USD",
    )
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    assert invoice.discount_minor == 300
    assert invoice.total_minor == 700


@pytest.mark.django_db
def test_ledger_and_balance(user):
    record_ledger_entry(user, LedgerEntryType.CREDIT, 500, "USD")
    record_ledger_entry(user, LedgerEntryType.DEBIT, -200, "USD")
    assert get_user_balance(user, "USD") == 300


@pytest.mark.django_db
def test_mark_invoice_paid_idempotent(user, price):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    mark_invoice_paid(invoice)
    balance_after_first = get_user_balance(user, "USD")
    mark_invoice_paid(invoice)
    assert get_user_balance(user, "USD") == balance_after_first
    invoice.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID

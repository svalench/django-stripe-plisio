from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction

from django_stripe_plisio.billing.enums import (
    DiscountType,
    InvoiceStatus,
    LedgerEntryType,
    PaymentProvider,
)
from django_stripe_plisio.billing.models import BalanceLedger, Invoice, PromoCode
from django_stripe_plisio.billing.money import minor_to_major_amount
from django_stripe_plisio.billing.services import (
    apply_promo,
    create_invoice,
    mark_invoice_paid,
    record_ledger_entry,
)
from django_stripe_plisio.exceptions import BillingError
from django_stripe_plisio.payments.services.invoice_sync import sync_pending_invoices
from django_stripe_plisio.payments.services.plisio_service import PlisioPaymentService
from django_stripe_plisio.payments.sync_types import InvoiceSyncOutcome


@pytest.fixture
def limited_promo(db):
    return PromoCode.objects.create(
        code="ONCE",
        discount_type=DiscountType.PERCENT,
        percent_value=10,
        max_uses=1,
    )


def test_public_api_importable():
    from django_stripe_plisio import api

    assert callable(api.create_checkout)
    assert callable(api.create_invoice)
    assert issubclass(api.BillingError, ValueError)


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (1999, "USD", "19.99"),
        (500, "JPY", "500"),
        (1234, "KWD", "1.234"),
        (123456789012345678, "USD", "1234567890123456.78"),
    ],
)
def test_minor_to_major_exact(amount, currency, expected):
    assert minor_to_major_amount(amount, currency) == expected


@pytest.mark.django_db
def test_pending_invoice_reserves_promo(user, price, limited_promo):
    create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="ONCE")
    with pytest.raises(BillingError, match="Invalid or expired"):
        create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="ONCE")


@pytest.mark.django_db
def test_expired_invoice_releases_promo(user, price, limited_promo):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="ONCE")
    Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.EXPIRED)
    create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="ONCE")


@pytest.mark.django_db
def test_payment_over_promo_limit_still_credited(user, price, limited_promo):
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="ONCE")
    PromoCode.objects.filter(pk=limited_promo.pk).update(used_count=1)

    mark_invoice_paid(invoice)

    invoice.refresh_from_db()
    limited_promo.refresh_from_db()
    assert invoice.status == InvoiceStatus.PAID
    assert limited_promo.used_count == 2


@pytest.mark.django_db
def test_promo_exact_case_wins(user, price):
    PromoCode.objects.create(code="SAVE", discount_type=DiscountType.PERCENT, percent_value=10)
    PromoCode.objects.create(code="save", discount_type=DiscountType.PERCENT, percent_value=50)

    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="save")

    assert invoice.discount_minor == 500


@pytest.mark.django_db
def test_promo_not_applicable_currency_rejected(user, price):
    PromoCode.objects.create(
        code="EURO",
        discount_type=DiscountType.FIXED,
        fixed_amount_minor=100,
        fixed_currency="EUR",
    )
    with pytest.raises(BillingError, match="not applicable"):
        create_invoice(user, price, provider=PaymentProvider.STRIPE, promo_code="EURO")


@pytest.mark.django_db
def test_apply_promo_blocked_after_checkout(user, price):
    PromoCode.objects.create(code="LATE", discount_type=DiscountType.PERCENT, percent_value=10)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)
    Invoice.objects.filter(pk=invoice.pk).update(external_id="cs_existing")

    with pytest.raises(BillingError, match="after checkout"):
        apply_promo(invoice, "LATE")


@pytest.mark.django_db
def test_apply_promo_updates_totals(user, price):
    PromoCode.objects.create(code="TEN", discount_type=DiscountType.PERCENT, percent_value=10)
    invoice = create_invoice(user, price, provider=PaymentProvider.STRIPE)

    invoice = apply_promo(invoice, "TEN")

    assert invoice.discount_minor == 100
    assert invoice.total_minor == 900


@pytest.mark.django_db
def test_ledger_reference_of_other_user_rejected(user):
    other = get_user_model().objects.create_user(username="other", password="x")
    record_ledger_entry(other, LedgerEntryType.CREDIT, 100, "USD", reference="ref-1")

    with pytest.raises(BillingError, match="another user"):
        record_ledger_entry(user, LedgerEntryType.CREDIT, 100, "USD", reference="ref-1")


@pytest.mark.django_db
def test_ledger_integrity_race_keeps_transaction_usable(user):
    existing = record_ledger_entry(user, LedgerEntryType.CREDIT, 100, "USD", reference="ref-race")
    # Эмуляция гонки: предварительная проверка не видит запись, INSERT ловит IntegrityError
    missing = MagicMock(first=MagicMock(return_value=None))

    with transaction.atomic():
        with patch.object(BalanceLedger.objects, "filter", return_value=missing):
            entry = record_ledger_entry(
                user,
                LedgerEntryType.CREDIT,
                100,
                "USD",
                reference="ref-race",
            )
        # Внешняя транзакция не сломана: запросы после IntegrityError работают
        assert BalanceLedger.objects.filter(reference="ref-race").count() == 1

    assert entry.pk == existing.pk


@pytest.mark.django_db
def test_sync_isolates_db_error_per_invoice(user, price, settings):
    settings.DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "k"
    first = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    second = create_invoice(user, price, provider=PaymentProvider.PLISIO)
    Invoice.objects.filter(pk__in=[first.pk, second.pk]).update(external_id="txn")

    def fake_sync(self, invoice):
        if invoice.pk == first.pk:
            # Ошибка БД внутри обработки одного счёта
            Invoice.objects.create(user=None, provider="plisio", currency="USD")
        mark_invoice_paid(invoice)
        return InvoiceSyncOutcome.PAID

    with patch.object(PlisioPaymentService, "sync_invoice_status", fake_sync):
        result = sync_pending_invoices()

    second.refresh_from_db()
    assert result.errors == 1
    assert result.paid == 1
    assert second.status == InvoiceStatus.PAID


@pytest.mark.django_db
def test_sync_command_force(settings):
    settings.DJANGO_STRIPE_PLISIO_CRON = {"sync_invoices": {"enabled": False}}
    with patch(
        "django_stripe_plisio.billing.management.commands.dsp_sync_invoices.sync_pending_invoices",
    ) as mock_sync:
        mock_sync.return_value = MagicMock(
            checked=0,
            paid=0,
            expired_remote=0,
            cancelled_remote=0,
            skipped=0,
            errors=0,
        )
        call_command("dsp_sync_invoices")
        mock_sync.assert_not_called()
        call_command("dsp_sync_invoices", "--force")
        mock_sync.assert_called_once()

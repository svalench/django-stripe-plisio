"""Сервисы биллинга: счета, скидки, ledger, entitlements."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from django_stripe_plisio.billing.enums import (
    DiscountType,
    InvoiceStatus,
    LedgerEntryType,
    PaymentProvider,
)
from django_stripe_plisio.billing.models import (
    BalanceLedger,
    DiscountGrant,
    Invoice,
    InvoiceDiscount,
    InvoiceLine,
    Price,
    PromoCode,
    UserEntitlement,
)
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.exceptions import BillingError
from django_stripe_plisio.signals import (
    balance_changed,
    entitlement_granted,
    invoice_paid,
    send_on_commit,
)

if TYPE_CHECKING:
    from django_stripe_plisio.types import UserType

logger = logging.getLogger(__name__)

PAYABLE_INVOICE_STATUSES = (InvoiceStatus.DRAFT, InvoiceStatus.PENDING)


def validate_currency(currency: str) -> str:
    currency = currency.upper()
    allowed = PackageSettings.allowed_currencies()
    if currency not in allowed:
        msg = f"Currency {currency} not allowed. Allowed: {allowed}"
        raise BillingError(msg)
    return currency


def validate_price_for_sale(price: Price) -> None:
    if not price.is_active:
        raise BillingError("Price is not active")
    if not price.product.is_active:
        raise BillingError("Product is not active")


def calculate_discount_minor(
    subtotal_minor: int,
    currency: str,
    discount_type: str,
    percent_value: int | None = None,
    fixed_amount_minor: int | None = None,
    fixed_currency: str = "",
) -> int:
    """Расчёт скидки в minor units."""
    if discount_type == DiscountType.PERCENT:
        if percent_value is None or percent_value <= 0:
            return 0
        return min(subtotal_minor, (subtotal_minor * percent_value) // 100)
    if discount_type == DiscountType.FIXED:
        if fixed_amount_minor is None:
            return 0
        if fixed_currency and fixed_currency.upper() != currency.upper():
            return 0
        return min(subtotal_minor, fixed_amount_minor)
    return 0


def _discount_from_source(
    source: PromoCode | DiscountGrant,
    subtotal_minor: int,
    currency: str,
) -> int:
    return calculate_discount_minor(
        subtotal_minor,
        currency,
        source.discount_type,
        source.percent_value,
        source.fixed_amount_minor,
        source.fixed_currency,
    )


def _promo_is_valid(promo: PromoCode) -> bool:
    now = timezone.now()
    if not promo.is_active:
        return False
    if promo.valid_from and promo.valid_from > now:
        return False
    if promo.valid_until and promo.valid_until < now:
        return False
    return True


def _promo_reserved_count(promo: PromoCode, exclude_invoice_id: int | None = None) -> int:
    """Неоплаченные счета с промокодом резервируют использование до оплаты или истечения."""
    qs = InvoiceDiscount.objects.filter(
        promo_code=promo,
        invoice__status__in=PAYABLE_INVOICE_STATUSES,
    )
    if exclude_invoice_id is not None:
        qs = qs.exclude(invoice_id=exclude_invoice_id)
    return qs.count()


def _promo_has_capacity(promo: PromoCode, exclude_invoice_id: int | None = None) -> bool:
    if promo.max_uses is None:
        return True
    reserved = _promo_reserved_count(promo, exclude_invoice_id)
    return promo.used_count + reserved < promo.max_uses


def _grant_is_valid(grant: DiscountGrant) -> bool:
    now = timezone.now()
    if not grant.is_active:
        return False
    if grant.valid_from and grant.valid_from > now:
        return False
    if grant.valid_until and grant.valid_until < now:
        return False
    return True


def resolve_promo_code(
    code: str,
    *,
    for_update: bool = False,
    exclude_invoice_id: int | None = None,
) -> PromoCode | None:
    """Найти действующий промокод со свободным лимитом.

    Точное совпадение приоритетнее регистронезависимого. ``for_update`` блокирует строку
    промокода, чтобы параллельные счета не превысили ``max_uses`` (нужна транзакция).
    """
    qs = PromoCode.objects.all()
    if for_update:
        qs = qs.select_for_update()
    promo = qs.filter(code=code).first() or qs.filter(code__iexact=code).order_by("pk").first()
    if promo is None or not _promo_is_valid(promo):
        return None
    if not _promo_has_capacity(promo, exclude_invoice_id):
        return None
    return promo


def resolve_private_grant(user: UserType) -> DiscountGrant | None:
    grants = DiscountGrant.objects.filter(user=user, is_active=True).order_by("-created_at")
    for grant in grants:
        if _grant_is_valid(grant):
            return grant
    return None


def _invoice_expires_at() -> datetime | None:
    ttl = PackageSettings.invoice_pending_ttl_hours()
    if ttl is None:
        return None
    return timezone.now() + timedelta(hours=int(ttl))


@transaction.atomic
def create_invoice(
    user: UserType,
    price: Price,
    provider: str,
    quantity: int = 1,
    promo_code: str | None = None,
    use_private_grant: bool = True,
    metadata: dict[str, Any] | None = None,
) -> Invoice:
    """Создание счёта со снимком цены и опциональной скидкой."""
    if quantity < 1:
        raise BillingError("quantity must be >= 1")
    if provider not in PaymentProvider.values:
        raise BillingError(f"Invalid provider: {provider}")

    price = Price.objects.select_related("product").get(pk=price.pk)
    validate_price_for_sale(price)

    currency = validate_currency(price.currency)
    subtotal = price.amount_minor * quantity

    promo_obj: PromoCode | None = None
    grant_obj: DiscountGrant | None = None
    discount_minor = 0

    if promo_code:
        promo_obj = resolve_promo_code(promo_code, for_update=True)
        if promo_obj is None:
            raise BillingError(f"Invalid or expired promo code: {promo_code}")
        discount_minor = _discount_from_source(promo_obj, subtotal, currency)
        if discount_minor == 0 and subtotal > 0:
            raise BillingError(f"Promo code {promo_code} is not applicable to this invoice")
    elif use_private_grant:
        grant_obj = resolve_private_grant(user)
        if grant_obj:
            discount_minor = _discount_from_source(grant_obj, subtotal, currency)

    invoice = Invoice.objects.create(
        user=user,
        status=InvoiceStatus.PENDING,
        provider=provider,
        currency=currency,
        subtotal_minor=subtotal,
        discount_minor=discount_minor,
        total_minor=max(0, subtotal - discount_minor),
        expires_at=_invoice_expires_at(),
        metadata=metadata or {},
    )

    InvoiceLine.objects.create(
        invoice=invoice,
        price=price,
        description=price.product.name,
        quantity=quantity,
        unit_amount_minor=price.amount_minor,
        line_total_minor=subtotal,
        currency=currency,
    )

    source = promo_obj or grant_obj
    if discount_minor > 0 and source is not None:
        InvoiceDiscount.objects.create(
            invoice=invoice,
            promo_code=promo_obj,
            discount_grant=grant_obj,
            discount_type=source.discount_type,
            amount_minor=discount_minor,
            currency=currency,
            label=promo_obj.code if promo_obj else (grant_obj.note if grant_obj else ""),
        )

    return invoice


@transaction.atomic
def apply_promo(invoice: Invoice, code: str) -> Invoice:
    """Применить промокод к существующему неоплаченному счёту (до создания checkout)."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in PAYABLE_INVOICE_STATUSES:
        raise BillingError("Cannot apply promo to non-pending invoice")
    if invoice.external_id:
        # Сумма уже передана провайдеру — изменение скидки разойдётся с суммой оплаты
        raise BillingError("Cannot apply promo after checkout was created")

    promo = resolve_promo_code(code, for_update=True, exclude_invoice_id=invoice.pk)
    if promo is None:
        raise BillingError(f"Invalid or expired promo code: {code}")

    discount_minor = _discount_from_source(promo, invoice.subtotal_minor, invoice.currency)
    if discount_minor == 0 and invoice.subtotal_minor > 0:
        raise BillingError(f"Promo code {code} is not applicable to this invoice")

    InvoiceDiscount.objects.filter(invoice=invoice).delete()
    InvoiceDiscount.objects.create(
        invoice=invoice,
        promo_code=promo,
        discount_type=promo.discount_type,
        amount_minor=discount_minor,
        currency=invoice.currency,
        label=promo.code,
    )

    invoice.discount_minor = discount_minor
    invoice.total_minor = max(0, invoice.subtotal_minor - discount_minor)
    invoice.save(update_fields=["discount_minor", "total_minor", "updated_at"])
    return invoice


def grant_private_discount(
    user: UserType,
    discount_type: str,
    percent_value: int | None = None,
    fixed_amount_minor: int | None = None,
    fixed_currency: str = "",
    valid_until: datetime | None = None,
    note: str = "",
    metadata: dict[str, Any] | None = None,
) -> DiscountGrant:
    """Выдать приватную скидку пользователю."""
    return DiscountGrant.objects.create(
        user=user,
        discount_type=discount_type,
        percent_value=percent_value,
        fixed_amount_minor=fixed_amount_minor,
        fixed_currency=fixed_currency.upper() if fixed_currency else "",
        valid_until=valid_until,
        note=note,
        metadata=metadata or {},
    )


def get_user_balance(user: UserType, currency: str) -> int:
    """Баланс пользователя в minor units по валюте."""
    currency = currency.upper()
    result = BalanceLedger.objects.filter(user=user, currency=currency).aggregate(
        total=Sum("amount_minor"),
    )
    return int(result["total"] or 0)


def _increment_promo_usage_for_invoice(invoice: Invoice) -> None:
    """Увеличить used_count промокода после успешной оплаты.

    Лимит проверяется при выставлении счёта; здесь деньги уже получены, поэтому
    превышение только логируется, а не блокирует зачисление.
    """
    discount = InvoiceDiscount.objects.filter(invoice=invoice, promo_code__isnull=False).first()
    if discount is None or discount.promo_code_id is None:
        return

    PromoCode.objects.filter(pk=discount.promo_code_id).update(used_count=F("used_count") + 1)
    promo = PromoCode.objects.get(pk=discount.promo_code_id)
    if promo.max_uses is not None and promo.used_count > promo.max_uses:
        logger.warning(
            "Promo code %s usage exceeded max_uses (%s > %s) by paid invoice %s",
            promo.code,
            promo.used_count,
            promo.max_uses,
            invoice.pk,
        )


def _ensure_ledger_owner(entry: BalanceLedger, user: UserType) -> BalanceLedger:
    if entry.user_id != user.pk:
        raise BillingError(f"Ledger reference {entry.reference} belongs to another user")
    return entry


@transaction.atomic
def record_ledger_entry(
    user: UserType,
    entry_type: str,
    amount_minor: int,
    currency: str,
    invoice: Invoice | None = None,
    reference: str = "",
    note: str = "",
    metadata: dict[str, Any] | None = None,
) -> BalanceLedger:
    """Запись проводки в ledger (append-only, идемпотентно по ``reference``)."""
    currency = validate_currency(currency)
    if reference:
        existing = BalanceLedger.objects.filter(reference=reference).first()
        if existing is not None:
            return _ensure_ledger_owner(existing, user)

    try:
        # Savepoint: после IntegrityError внешняя транзакция PostgreSQL остаётся рабочей
        with transaction.atomic():
            entry = BalanceLedger.objects.create(
                user=user,
                entry_type=entry_type,
                amount_minor=amount_minor,
                currency=currency,
                invoice=invoice,
                reference=reference,
                note=note,
                metadata=metadata or {},
            )
    except IntegrityError:
        if not reference:
            raise
        return _ensure_ledger_owner(BalanceLedger.objects.get(reference=reference), user)

    send_on_commit(
        balance_changed,
        sender=BalanceLedger,
        user=user,
        entry=entry,
        balance=get_user_balance(user, currency),
        currency=currency,
    )
    return entry


def _grant_entitlement(invoice: Invoice) -> None:
    line = InvoiceLine.objects.filter(invoice=invoice).select_related("price__product").first()
    if line is None or line.price is None:
        return
    entitlement = UserEntitlement.objects.create(
        user=invoice.user,
        product=line.price.product,
        invoice=invoice,
        source=invoice.provider,
        metadata={"invoice_id": invoice.pk},
    )
    send_on_commit(
        entitlement_granted,
        sender=UserEntitlement,
        entitlement=entitlement,
        invoice=invoice,
    )


@transaction.atomic
def mark_invoice_paid(invoice: Invoice, external_id: str = "") -> Invoice:
    """Отметить счёт оплаченным и начислить баланс/entitlement (идемпотентно)."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)

    if invoice.status == InvoiceStatus.PAID:
        return invoice
    if invoice.status not in PAYABLE_INVOICE_STATUSES:
        # Оплата пришла после истечения/отмены: деньги получены, счёт всё равно закрываем
        logger.warning("Invoice %s paid in status %s", invoice.pk, invoice.status)

    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = timezone.now()
    if external_id:
        invoice.external_id = external_id
    invoice.save(update_fields=["status", "paid_at", "external_id", "updated_at"])

    _increment_promo_usage_for_invoice(invoice)

    record_ledger_entry(
        user=invoice.user,
        entry_type=LedgerEntryType.CREDIT,
        amount_minor=invoice.total_minor,
        currency=invoice.currency,
        invoice=invoice,
        reference=f"invoice:{invoice.pk}",
        note="Payment received",
    )

    _grant_entitlement(invoice)

    send_on_commit(invoice_paid, sender=Invoice, invoice=invoice)
    return invoice


def expire_pending_invoices() -> int:
    """Перевести просроченные pending-счета в expired."""
    now = timezone.now()
    qs = Invoice.objects.filter(
        status=InvoiceStatus.PENDING,
        expires_at__isnull=False,
        expires_at__lt=now,
    )
    return qs.update(status=InvoiceStatus.EXPIRED, updated_at=now)

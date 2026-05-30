"""Сервисы биллинга: счета, скидки, ledger, entitlements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Sum
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
from django_stripe_plisio.signals import balance_changed, entitlement_granted, invoice_paid

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


def validate_currency(currency: str) -> str:
    currency = currency.upper()
    allowed = PackageSettings.allowed_currencies()
    if currency not in allowed:
        msg = f"Currency {currency} not allowed. Allowed: {allowed}"
        raise ValueError(msg)
    return currency


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


def _promo_is_valid(promo: PromoCode) -> bool:
    now = timezone.now()
    if not promo.is_active:
        return False
    if promo.valid_from and promo.valid_from > now:
        return False
    if promo.valid_until and promo.valid_until < now:
        return False
    if promo.max_uses is not None and promo.used_count >= promo.max_uses:
        return False
    return True


def _grant_is_valid(grant: DiscountGrant) -> bool:
    now = timezone.now()
    if not grant.is_active:
        return False
    if grant.valid_from and grant.valid_from > now:
        return False
    if grant.valid_until and grant.valid_until < now:
        return False
    return True


def resolve_promo_code(code: str) -> PromoCode | None:
    try:
        promo = PromoCode.objects.get(code__iexact=code)
    except PromoCode.DoesNotExist:
        return None
    if not _promo_is_valid(promo):
        return None
    return promo


def resolve_private_grant(user: AbstractBaseUser) -> DiscountGrant | None:
    grants = DiscountGrant.objects.filter(user=user, is_active=True).order_by("-created_at")
    for grant in grants:
        if _grant_is_valid(grant):
            return grant
    return None


@transaction.atomic
def create_invoice(
    user: AbstractBaseUser,
    price: Price,
    provider: str,
    quantity: int = 1,
    promo_code: str | None = None,
    use_private_grant: bool = True,
    metadata: dict[str, Any] | None = None,
) -> Invoice:
    """Создание счёта со снимком цены и опциональной скидкой."""
    if quantity < 1:
        raise ValueError("quantity must be >= 1")
    if provider not in PaymentProvider.values:
        raise ValueError(f"Invalid provider: {provider}")

    currency = validate_currency(price.currency)
    subtotal = price.amount_minor * quantity

    invoice = Invoice.objects.create(
        user=user,
        status=InvoiceStatus.PENDING,
        provider=provider,
        currency=currency,
        subtotal_minor=subtotal,
        discount_minor=0,
        total_minor=subtotal,
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

    discount_minor = 0
    promo_obj: PromoCode | None = None
    grant_obj: DiscountGrant | None = None

    if promo_code:
        promo_obj = resolve_promo_code(promo_code)
        if promo_obj is None:
            raise ValueError(f"Invalid or expired promo code: {promo_code}")
        discount_minor = calculate_discount_minor(
            subtotal,
            currency,
            promo_obj.discount_type,
            promo_obj.percent_value,
            promo_obj.fixed_amount_minor,
            promo_obj.fixed_currency,
        )
    elif use_private_grant:
        grant_obj = resolve_private_grant(user)
        if grant_obj:
            discount_minor = calculate_discount_minor(
                subtotal,
                currency,
                grant_obj.discount_type,
                grant_obj.percent_value,
                grant_obj.fixed_amount_minor,
                grant_obj.fixed_currency,
            )

    if discount_minor > 0:
        discount_type = (
            promo_obj.discount_type
            if promo_obj
            else (grant_obj.discount_type if grant_obj else DiscountType.FIXED)
        )
        label = promo_obj.code if promo_obj else (grant_obj.note if grant_obj else "")
        InvoiceDiscount.objects.create(
            invoice=invoice,
            promo_code=promo_obj,
            discount_grant=grant_obj,
            discount_type=discount_type,
            amount_minor=discount_minor,
            currency=currency,
            label=label,
        )
        if promo_obj:
            PromoCode.objects.filter(pk=promo_obj.pk).update(used_count=promo_obj.used_count + 1)

    total = max(0, subtotal - discount_minor)
    invoice.discount_minor = discount_minor
    invoice.total_minor = total
    invoice.save(update_fields=["discount_minor", "total_minor", "updated_at"])

    return invoice


def apply_promo(invoice: Invoice, code: str) -> Invoice:
    """Применить промокод к существующему неоплаченному счёту."""
    if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.PENDING):
        raise ValueError("Cannot apply promo to non-pending invoice")

    promo = resolve_promo_code(code)
    if promo is None:
        raise ValueError(f"Invalid or expired promo code: {code}")

    discount_minor = calculate_discount_minor(
        invoice.subtotal_minor,
        invoice.currency,
        promo.discount_type,
        promo.percent_value,
        promo.fixed_amount_minor,
        promo.fixed_currency,
    )

    InvoiceDiscount.objects.filter(invoice=invoice).delete()
    InvoiceDiscount.objects.create(
        invoice=invoice,
        promo_code=promo,
        discount_type=promo.discount_type,
        amount_minor=discount_minor,
        currency=invoice.currency,
        label=promo.code,
    )
    PromoCode.objects.filter(pk=promo.pk).update(used_count=promo.used_count + 1)

    invoice.discount_minor = discount_minor
    invoice.total_minor = max(0, invoice.subtotal_minor - discount_minor)
    invoice.save(update_fields=["discount_minor", "total_minor", "updated_at"])
    return invoice


def grant_private_discount(
    user: AbstractBaseUser,
    discount_type: str,
    percent_value: int | None = None,
    fixed_amount_minor: int | None = None,
    fixed_currency: str = "",
    valid_until=None,
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


def get_user_balance(user: AbstractBaseUser, currency: str) -> int:
    """Баланс пользователя в minor units по валюте."""
    currency = currency.upper()
    result = BalanceLedger.objects.filter(user=user, currency=currency).aggregate(
        total=Sum("amount_minor"),
    )
    return int(result["total"] or 0)


@transaction.atomic
def record_ledger_entry(
    user: AbstractBaseUser,
    entry_type: str,
    amount_minor: int,
    currency: str,
    invoice: Invoice | None = None,
    reference: str = "",
    note: str = "",
    metadata: dict[str, Any] | None = None,
) -> BalanceLedger:
    """Запись проводки в ledger (append-only)."""
    currency = validate_currency(currency)
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
    balance_changed.send(
        sender=BalanceLedger,
        user=user,
        entry=entry,
        balance=get_user_balance(user, currency),
        currency=currency,
    )
    return entry


@transaction.atomic
def mark_invoice_paid(invoice: Invoice, external_id: str = "") -> Invoice:
    """Отметить счёт оплаченным и начислить баланс/entitlement."""
    if invoice.status == InvoiceStatus.PAID:
        return invoice

    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = timezone.now()
    if external_id:
        invoice.external_id = external_id
    invoice.save(update_fields=["status", "paid_at", "external_id", "updated_at"])

    record_ledger_entry(
        user=invoice.user,
        entry_type=LedgerEntryType.CREDIT,
        amount_minor=invoice.total_minor,
        currency=invoice.currency,
        invoice=invoice,
        reference=f"invoice:{invoice.pk}",
        note="Payment received",
    )

    line = InvoiceLine.objects.filter(invoice=invoice).select_related("price__product").first()
    if line and line.price and line.price.product:
        entitlement = UserEntitlement.objects.create(
            user=invoice.user,
            product=line.price.product,
            invoice=invoice,
            source=invoice.provider,
            metadata={"invoice_id": invoice.pk},
        )
        entitlement_granted.send(sender=UserEntitlement, entitlement=entitlement, invoice=invoice)

    invoice_paid.send(sender=Invoice, invoice=invoice)
    return invoice

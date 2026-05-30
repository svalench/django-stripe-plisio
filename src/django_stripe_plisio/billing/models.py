"""Модели каталога, счетов, скидок, ledger и entitlements."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from django_stripe_plisio.billing.enums import (
    BillingPeriod,
    DiscountType,
    InvoiceStatus,
    LedgerEntryType,
    PaymentProvider,
)


class Product(models.Model):
    """Бизнес-продукт или тариф."""

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return self.name


class Price(models.Model):
    """Цена продукта в minor units + валюта."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="prices")
    currency = models.CharField(max_length=3)
    amount_minor = models.PositiveBigIntegerField(
        help_text="Сумма в минимальных единицах (центы, копейки и т.д.)",
    )
    billing_period = models.CharField(
        max_length=16,
        choices=BillingPeriod.choices,
        default=BillingPeriod.ONE_TIME,
    )
    is_active = models.BooleanField(default=True)
    stripe_product_id = models.CharField(max_length=255, blank=True)
    stripe_price_id = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product", "currency", "amount_minor"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "currency", "billing_period", "amount_minor"],
                name="dsp_unique_price_variant",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product.code} {self.amount_minor} {self.currency}"


class PromoCode(models.Model):
    """Публичный промокод."""

    code = models.CharField(max_length=64, unique=True, db_index=True)
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices)
    percent_value = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Процент скидки 1-100",
    )
    fixed_amount_minor = models.PositiveBigIntegerField(null=True, blank=True)
    fixed_currency = models.CharField(max_length=3, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.code


class DiscountGrant(models.Model):
    """Приватная скидка для конкретного пользователя."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discount_grants",
    )
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices)
    percent_value = models.PositiveSmallIntegerField(null=True, blank=True)
    fixed_amount_minor = models.PositiveBigIntegerField(null=True, blank=True)
    fixed_currency = models.CharField(max_length=3, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Grant #{self.pk} user={self.user_id}"


class Invoice(models.Model):
    """Счёт пользователю."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_invoices",
    )
    status = models.CharField(
        max_length=16,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.DRAFT,
        db_index=True,
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices)
    currency = models.CharField(max_length=3)
    subtotal_minor = models.PositiveBigIntegerField(default=0)
    discount_minor = models.PositiveBigIntegerField(default=0)
    total_minor = models.PositiveBigIntegerField(default=0)
    payment_url = models.URLField(blank=True)
    external_id = models.CharField(max_length=255, blank=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["provider", "status"]),
        ]

    def __str__(self) -> str:
        return f"Invoice #{self.pk} {self.status}"

    @property
    def is_paid(self) -> bool:
        return self.status == InvoiceStatus.PAID


class InvoiceLine(models.Model):
    """Позиция счёта со снимком цены."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    price = models.ForeignKey(
        Price,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_lines",
    )
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_amount_minor = models.PositiveBigIntegerField()
    line_total_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.description} x{self.quantity}"


class InvoiceDiscount(models.Model):
    """Снимок применённой скидки на момент выставления счёта."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="discounts")
    promo_code = models.ForeignKey(
        PromoCode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    discount_grant = models.ForeignKey(
        DiscountGrant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    label = models.CharField(max_length=128, blank=True)

    def __str__(self) -> str:
        return f"Discount {self.amount_minor} {self.currency}"


class UserEntitlement(models.Model):
    """Выданный доступ по оплате или подписке."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="entitlements",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entitlements",
    )
    active_from = models.DateTimeField(default=timezone.now)
    active_until = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-active_from"]

    def __str__(self) -> str:
        return f"Entitlement user={self.user_id} product={self.product_id}"

    @property
    def is_active(self) -> bool:
        now = timezone.now()
        if self.active_from > now:
            return False
        if self.active_until and self.active_until < now:
            return False
        return True


class BalanceLedger(models.Model):
    """Append-only проводки баланса."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="balance_ledger",
    )
    entry_type = models.CharField(max_length=16, choices=LedgerEntryType.choices)
    amount_minor = models.BigIntegerField(
        help_text="Положительное — пополнение, отрицательное — списание",
    )
    currency = models.CharField(max_length=3)
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    reference = models.CharField(max_length=128, blank=True)
    note = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "currency"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reference"],
                condition=models.Q(reference__gt=""),
                name="dsp_unique_ledger_reference",
            ),
        ]

    def __str__(self) -> str:
        return f"Ledger {self.entry_type} {self.amount_minor} {self.currency}"

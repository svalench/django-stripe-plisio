from django.contrib import admin, messages
from django.contrib.auth import get_user_model

from django_stripe_plisio.billing.enums import InvoiceStatus
from django_stripe_plisio.billing.models import (
    BalanceLedger,
    DiscountGrant,
    Invoice,
    InvoiceDiscount,
    InvoiceLine,
    Price,
    Product,
    PromoCode,
    UserEntitlement,
)
from django_stripe_plisio.billing.services import mark_invoice_paid


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    readonly_fields = (
        "description",
        "quantity",
        "unit_amount_minor",
        "line_total_minor",
        "currency",
    )


class InvoiceDiscountInline(admin.TabularInline):
    model = InvoiceDiscount
    extra = 0
    readonly_fields = ("discount_type", "amount_minor", "currency", "label")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = ("product", "currency", "amount_minor", "billing_period", "is_active")
    list_filter = ("currency", "billing_period", "is_active")
    search_fields = ("product__code", "stripe_price_id")


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "discount_type",
        "percent_value",
        "fixed_amount_minor",
        "is_active",
        "used_count",
    )
    list_filter = ("discount_type", "is_active")
    search_fields = ("code",)


@admin.register(DiscountGrant)
class DiscountGrantAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "discount_type",
        "percent_value",
        "fixed_amount_minor",
        "is_active",
        "created_at",
    )
    list_filter = ("discount_type", "is_active")
    raw_id_fields = ("user",)


def _user_search_field() -> str:
    return f"user__{get_user_model().USERNAME_FIELD}"


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "provider",
        "status",
        "currency",
        "total_minor",
        "paid_at",
        "created_at",
    )
    list_filter = ("provider", "status", "currency")
    raw_id_fields = ("user",)
    # Статус меняется только сервисами: ручной paid без ledger/entitlement ломает учёт
    readonly_fields = (
        "status",
        "subtotal_minor",
        "discount_minor",
        "total_minor",
        "paid_at",
        "created_at",
        "updated_at",
    )
    inlines = [InvoiceLineInline, InvoiceDiscountInline]
    actions = ["mark_paid"]

    def get_search_fields(self, request):
        return (_user_search_field(), "external_id")

    @admin.action(description="Отметить оплаченными (ledger + entitlement)")
    def mark_paid(self, request, queryset):
        count = 0
        for invoice in queryset.exclude(status=InvoiceStatus.PAID):
            mark_invoice_paid(invoice)
            count += 1
        self.message_user(request, f"Оплаченными отмечено счетов: {count}", messages.SUCCESS)


@admin.register(UserEntitlement)
class UserEntitlementAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "active_from", "active_until", "source")
    list_filter = ("source",)
    raw_id_fields = ("user", "invoice")


@admin.register(BalanceLedger)
class BalanceLedgerAdmin(admin.ModelAdmin):
    list_display = ("user", "entry_type", "amount_minor", "currency", "reference", "created_at")
    list_filter = ("entry_type", "currency")
    raw_id_fields = ("user", "invoice")
    readonly_fields = (
        "user",
        "entry_type",
        "amount_minor",
        "currency",
        "invoice",
        "reference",
        "note",
        "metadata",
        "created_at",
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

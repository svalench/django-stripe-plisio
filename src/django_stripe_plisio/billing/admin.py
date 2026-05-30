from django.contrib import admin

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
    search_fields = ("user__username", "external_id")
    raw_id_fields = ("user",)
    readonly_fields = (
        "subtotal_minor",
        "discount_minor",
        "total_minor",
        "paid_at",
        "created_at",
        "updated_at",
    )
    inlines = [InvoiceLineInline, InvoiceDiscountInline]


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

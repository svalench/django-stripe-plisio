from django.contrib import admin

from django_stripe_plisio.payments.models import (
    PaymentAttempt,
    PlisioPaymentAttempt,
    ProviderTransaction,
    StripePaymentAttempt,
    StripeSubscription,
    WebhookEvent,
)


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "provider", "status", "external_id", "created_at")
    list_filter = ("provider", "status")
    search_fields = ("external_id", "invoice__id")
    raw_id_fields = ("invoice",)
    readonly_fields = ("request_payload", "response_payload", "created_at", "updated_at")


@admin.register(StripePaymentAttempt)
class StripePaymentAttemptAdmin(PaymentAttemptAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(provider="stripe")


@admin.register(PlisioPaymentAttempt)
class PlisioPaymentAttemptAdmin(PaymentAttemptAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(provider="plisio")


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "event_type", "status", "idempotency_key", "created_at")
    list_filter = ("provider", "status", "event_type")
    readonly_fields = ("payload", "idempotency_key", "created_at", "processed_at")


@admin.register(ProviderTransaction)
class ProviderTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "external_id",
        "invoice",
        "amount_minor",
        "currency",
        "status",
        "created_at",
    )
    list_filter = ("provider", "status", "currency")
    readonly_fields = ("raw_payload", "created_at")


@admin.register(StripeSubscription)
class StripeSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "stripe_subscription_id", "status", "current_period_end")
    list_filter = ("status",)
    raw_id_fields = ("user", "price")

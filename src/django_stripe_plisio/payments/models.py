"""Модели попыток оплаты, webhooks и подписок Stripe."""

from django.conf import settings
from django.db import models

from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.billing.models import Invoice, Price
from django_stripe_plisio.payments.enums import (
    PaymentAttemptStatus,
    StripeSubscriptionStatus,
    WebhookProcessingStatus,
)


class PaymentAttempt(models.Model):
    """Каждая попытка оплаты по счёту."""

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name="payment_attempts",
    )
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=PaymentAttemptStatus.choices,
        default=PaymentAttemptStatus.CREATED,
        db_index=True,
    )
    external_id = models.CharField(max_length=255, blank=True, db_index=True)
    payment_url = models.URLField(blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["provider", "status"]),
        ]

    def __str__(self) -> str:
        return f"Attempt #{self.pk} {self.provider} {self.status}"


class StripePaymentAttempt(PaymentAttempt):
    """Proxy для admin-фильтрации Stripe."""

    class Meta:
        proxy = True
        verbose_name = "Stripe payment attempt"
        verbose_name_plural = "Stripe payment attempts"


class PlisioPaymentAttempt(PaymentAttempt):
    """Proxy для admin-фильтрации Plisio."""

    class Meta:
        proxy = True
        verbose_name = "Plisio payment attempt"
        verbose_name_plural = "Plisio payment attempts"


class WebhookEvent(models.Model):
    """Сырые webhook-события с идемпотентностью."""

    provider = models.CharField(max_length=16, choices=PaymentProvider.choices, db_index=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=128, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=WebhookProcessingStatus.choices,
        default=WebhookProcessingStatus.RECEIVED,
    )
    error_message = models.TextField(blank=True)
    payment_attempt = models.ForeignKey(
        PaymentAttempt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Webhook {self.provider} {self.idempotency_key}"


class ProviderTransaction(models.Model):
    """Нормализованная транзакция провайдера."""

    provider = models.CharField(max_length=16, choices=PaymentProvider.choices, db_index=True)
    external_id = models.CharField(max_length=255, db_index=True)
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_transactions",
    )
    payment_attempt = models.ForeignKey(
        PaymentAttempt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
    )
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_id"],
                name="dsp_unique_provider_transaction",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.external_id}"


class StripeSubscription(models.Model):
    """Stripe recurring subscription."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stripe_subscriptions",
    )
    price = models.ForeignKey(
        Price,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    stripe_subscription_id = models.CharField(max_length=255, unique=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=32,
        choices=StripeSubscriptionStatus.choices,
        default=StripeSubscriptionStatus.INCOMPLETE,
    )
    current_period_end = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"StripeSub {self.stripe_subscription_id}"

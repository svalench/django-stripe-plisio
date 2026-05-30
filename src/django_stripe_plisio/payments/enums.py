from django.db import models


class PaymentAttemptStatus(models.TextChoices):
    CREATED = "created", "Created"
    PENDING = "pending", "Pending"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class WebhookProcessingStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class StripeSubscriptionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past due"
    CANCELED = "canceled", "Canceled"
    INCOMPLETE = "incomplete", "Incomplete"
    TRIALING = "trialing", "Trialing"

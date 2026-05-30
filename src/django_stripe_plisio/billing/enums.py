from django.db import models


class BillingPeriod(models.TextChoices):
    ONE_TIME = "one_time", "One time"
    MONTH = "month", "Month"
    YEAR = "year", "Year"


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


class PaymentProvider(models.TextChoices):
    STRIPE = "stripe", "Stripe"
    PLISIO = "plisio", "Plisio"


class LedgerEntryType(models.TextChoices):
    CREDIT = "credit", "Credit"
    DEBIT = "debit", "Debit"
    REFUND = "refund", "Refund"
    ADJUSTMENT = "adjustment", "Adjustment"
    PROMO = "promo", "Promo"


class DiscountType(models.TextChoices):
    PERCENT = "percent", "Percent"
    FIXED = "fixed", "Fixed"

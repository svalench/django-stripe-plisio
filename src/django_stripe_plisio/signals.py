"""Сигналы для интеграции с проектами-потребителями."""

from django.dispatch import Signal

invoice_paid = Signal()
payment_failed = Signal()
balance_changed = Signal()
entitlement_granted = Signal()

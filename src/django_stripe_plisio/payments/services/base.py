"""Базовый интерфейс платёжных провайдеров."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_stripe_plisio.billing.models import Invoice
    from django_stripe_plisio.payments.models import PaymentAttempt


class BasePaymentProvider(ABC):
    provider: str

    @abstractmethod
    def create_checkout(self, invoice: Invoice) -> PaymentAttempt:
        """Создать сессию/инвойс оплаты у провайдера."""

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        """Проверить подпись webhook и вернуть распарсенные данные."""

    @abstractmethod
    def handle_webhook_event(self, event_data: dict) -> None:
        """Обработать событие webhook."""

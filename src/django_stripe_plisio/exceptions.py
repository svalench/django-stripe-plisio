"""Исключения пакета."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_stripe_plisio.payments.models import PaymentAttempt


class WebhookVerificationError(Exception):
    """Ошибка проверки подписи webhook."""


class BillingError(ValueError):
    """Бизнес-ошибка биллинга: неверные входные данные или недопустимое состояние счёта.

    Наследуется от ValueError для обратной совместимости с кодом, ловившим ValueError.
    """


class PaymentProviderError(Exception):
    """Провайдер не смог создать оплату; попытка с деталями сохранена в БД."""

    def __init__(self, message: str, attempt: PaymentAttempt | None = None) -> None:
        super().__init__(message)
        self.attempt = attempt

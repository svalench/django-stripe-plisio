"""Сигналы для интеграции с проектами-потребителями."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.dispatch import Signal

logger = logging.getLogger(__name__)

invoice_paid = Signal()
payment_failed = Signal()
balance_changed = Signal()
entitlement_granted = Signal()
# Сумма/валюта оплаты не совпала со счётом (Stripe amount_total, Plisio mismatch/partial)
payment_mismatch = Signal()
# Изменение статуса/периода подписки Stripe (в т.ч. продление и отмена)
subscription_changed = Signal()


def send_on_commit(signal: Signal, sender: Any, **kwargs: Any) -> None:
    """Отправить сигнал после коммита транзакции.

    Ошибки обработчиков логируются и не влияют на уже зафиксированный платёж.
    """

    def _send() -> None:
        for receiver, response in signal.send_robust(sender=sender, **kwargs):
            if isinstance(response, Exception):
                logger.error(
                    "Signal receiver %r failed",
                    receiver,
                    exc_info=(type(response), response, response.__traceback__),
                )

    transaction.on_commit(_send)

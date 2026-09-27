"""Базовый интерфейс платёжных провайдеров."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from django_stripe_plisio.billing.models import Invoice
from django_stripe_plisio.payments.enums import WebhookProcessingStatus
from django_stripe_plisio.payments.models import PaymentAttempt, WebhookEvent

if TYPE_CHECKING:
    from django_stripe_plisio.payments.sync_types import InvoiceSyncOutcome

logger = logging.getLogger(__name__)


class BasePaymentProvider(ABC):
    provider: str

    @abstractmethod
    def create_checkout(self, invoice: Invoice) -> PaymentAttempt:
        """Создать сессию/инвойс оплаты у провайдера.

        Ошибки провайдера не бросаются: возвращается попытка со статусом FAILED,
        чтобы она сохранилась в БД. Вызывать через ``payments.services.create_checkout``.
        """

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        """Проверить подпись webhook и вернуть распарсенные данные."""

    @abstractmethod
    def handle_webhook_event(self, event_data: dict) -> None:
        """Обработать событие webhook."""

    @abstractmethod
    def sync_invoice_status(self, invoice: Invoice) -> InvoiceSyncOutcome:
        """Опросить провайдера и обновить статус счёта."""

    def _process_webhook(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        payload: dict[str, Any],
        handler: Callable[[], str],
    ) -> None:
        """Идемпотентная обработка события; ``handler`` возвращает PROCESSED или SKIPPED.

        Запись WebhookEvent создаётся вне транзакции обработчика, поэтому статус FAILED
        сохраняется даже при откате изменений обработчика.
        """
        webhook, _ = WebhookEvent.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "provider": self.provider,
                "event_type": event_type,
                "payload": payload,
            },
        )
        try:
            with transaction.atomic():
                locked = WebhookEvent.objects.select_for_update().get(pk=webhook.pk)
                if locked.status == WebhookProcessingStatus.PROCESSED:
                    return
                locked.status = handler()
                locked.processed_at = timezone.now()
                locked.error_message = ""
                locked.save(update_fields=["status", "processed_at", "error_message"])
        except Exception as exc:
            WebhookEvent.objects.filter(pk=webhook.pk).update(
                status=WebhookProcessingStatus.FAILED,
                error_message=str(exc),
            )
            logger.exception("%s webhook processing failed (%s)", self.provider, idempotency_key)
            raise

    def _latest_attempt(
        self,
        invoice: Invoice,
        statuses: Iterable[str] | None = None,
    ) -> PaymentAttempt | None:
        qs = PaymentAttempt.objects.filter(invoice=invoice, provider=self.provider)
        if statuses is not None:
            qs = qs.filter(status__in=list(statuses))
        return qs.order_by("-created_at", "-pk").first()

    def _find_invoice(self, invoice_id: Any) -> Invoice | None:
        """Счёт этого провайдера по id из metadata/order_number."""
        try:
            invoice = Invoice.objects.get(pk=int(invoice_id))
        except (Invoice.DoesNotExist, TypeError, ValueError):
            logger.warning("%s: invoice %r not found", self.provider, invoice_id)
            return None
        if invoice.provider != self.provider:
            logger.warning(
                "%s: invoice %s belongs to provider %s",
                self.provider,
                invoice.pk,
                invoice.provider,
            )
            return None
        return invoice

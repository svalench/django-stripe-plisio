"""Базовый интерфейс платёжных провайдеров."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from django_stripe_plisio.billing.models import Invoice
from django_stripe_plisio.payments.enums import WebhookProcessingStatus
from django_stripe_plisio.payments.models import PaymentAttempt, WebhookEvent
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
    def load_remote_invoice_state(self, invoice: Invoice) -> Any:
        """HTTP к провайдеру без транзакции. Может вернуть ``InvoiceSyncOutcome``."""

    @abstractmethod
    def apply_remote_invoice_state(self, invoice: Invoice, remote: Any) -> InvoiceSyncOutcome:
        """Записать уже полученный статус. Вызывается внутри короткой транзакции."""

    def sync_invoice_status(self, invoice: Invoice) -> InvoiceSyncOutcome:
        """Опросить провайдера и обновить статус счёта.

        Сетевой вызов снаружи транзакции, запись статуса — под блокировкой строки.
        """
        remote = self.load_remote_invoice_state(invoice)
        if isinstance(remote, InvoiceSyncOutcome):
            return remote
        with transaction.atomic():
            current = Invoice.objects.select_for_update().get(pk=invoice.pk)
            return self.apply_remote_invoice_state(current, remote)

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
                invoice, attempt = self._webhook_subject(payload)
                update_fields = ["status", "processed_at", "error_message"]
                if invoice is not None:
                    locked.invoice = invoice
                    update_fields.append("invoice")
                if attempt is not None:
                    locked.payment_attempt = attempt
                    update_fields.append("payment_attempt")
                locked.processed_at = timezone.now()
                locked.error_message = ""
                locked.save(update_fields=update_fields)
        except Exception as exc:
            WebhookEvent.objects.filter(pk=webhook.pk).update(
                status=WebhookProcessingStatus.FAILED,
                error_message=str(exc),
            )
            logger.exception("%s webhook processing failed (%s)", self.provider, idempotency_key)
            raise

    def _webhook_subject(
        self,
        payload: dict[str, Any],
    ) -> tuple[Invoice | None, PaymentAttempt | None]:
        """Счёт и попытка, к которым относится уже обработанный webhook."""
        return None, None

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

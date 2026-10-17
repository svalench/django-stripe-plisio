"""Опрос статусов pending-счетов у провайдеров (догонка после пропущенных webhooks)."""

from __future__ import annotations

import logging

from django.db import transaction

from django_stripe_plisio.billing.enums import InvoiceStatus
from django_stripe_plisio.billing.models import Invoice
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.payments.sync_types import InvoiceSyncOutcome, SyncResult

logger = logging.getLogger(__name__)


def _pending_invoices_queryset():
    qs = Invoice.objects.filter(
        status=InvoiceStatus.PENDING,
    ).exclude(external_id="")
    providers = PackageSettings.invoice_sync_providers()
    if providers is not None:
        qs = qs.filter(provider__in=providers)
    return qs.order_by("pk")


def sync_pending_invoices(*, batch_size: int | None = None, dry_run: bool = False) -> SyncResult:
    """Опросить провайдеров по pending-счетам с external_id.

    HTTP к провайдеру выполняется вне транзакции. Запись статуса — короткими
    транзакциями внутри сервиса. Ошибка одного счёта не ломает остальные.
    """
    from django_stripe_plisio.payments.services import get_payment_service

    limit = batch_size if batch_size is not None else PackageSettings.invoice_sync_batch_size()
    result = SyncResult()

    invoice_ids = list(_pending_invoices_queryset().values_list("pk", flat=True)[:limit])

    for invoice_id in invoice_ids:
        invoice = Invoice.objects.filter(pk=invoice_id, status=InvoiceStatus.PENDING).first()
        if invoice is None:
            continue
        result.checked += 1

        if dry_run:
            logger.info("dry-run: would sync invoice %s (%s)", invoice.pk, invoice.provider)
            continue

        try:
            service = get_payment_service(invoice.provider)
            remote = service.load_remote_invoice_state(invoice)
            if isinstance(remote, InvoiceSyncOutcome):
                outcome = remote
            else:
                with transaction.atomic():
                    current = Invoice.objects.select_for_update().get(pk=invoice.pk)
                    outcome = service.apply_remote_invoice_state(current, remote)
        except Exception:
            logger.exception("Invoice sync failed for invoice %s", invoice.pk)
            result.errors += 1
            continue

        if outcome == InvoiceSyncOutcome.PAID:
            result.paid += 1
        elif outcome == InvoiceSyncOutcome.EXPIRED:
            result.expired_remote += 1
        elif outcome == InvoiceSyncOutcome.CANCELLED:
            result.cancelled_remote += 1
        elif outcome == InvoiceSyncOutcome.SKIPPED:
            result.skipped += 1
        elif outcome == InvoiceSyncOutcome.ERROR:
            result.errors += 1

    return result

"""Plisio crypto invoices и callbacks."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlencode

import requests
from django.utils import timezone

from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.billing.models import Invoice
from django_stripe_plisio.billing.services import mark_invoice_paid
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.payments.enums import PaymentAttemptStatus, WebhookProcessingStatus
from django_stripe_plisio.payments.models import PaymentAttempt, ProviderTransaction, WebhookEvent
from django_stripe_plisio.payments.services.base import BasePaymentProvider
from django_stripe_plisio.signals import payment_failed

PLISIO_API_URL = "https://api.plisio.net/api/v1"


class PlisioPaymentService(BasePaymentProvider):
    provider = PaymentProvider.PLISIO

    def _api_key(self) -> str:
        return PackageSettings.plisio_api_key()

    def create_checkout(self, invoice: Invoice) -> PaymentAttempt:
        attempt = PaymentAttempt.objects.create(
            invoice=invoice,
            provider=self.provider,
            status=PaymentAttemptStatus.CREATED,
        )

        # Сумма для Plisio в основных единицах (API ожидает decimal string)
        amount = f"{invoice.total_minor / 100:.2f}"

        params: dict[str, Any] = {
            "api_key": self._api_key(),
            "source_currency": invoice.currency,
            "source_amount": amount,
            "order_number": str(invoice.pk),
            "order_name": f"Invoice {invoice.pk}",
            "callback_url": PackageSettings.success_url() or "",
        }

        attempt.request_payload = {k: v for k, v in params.items() if k != "api_key"}

        try:
            response = requests.get(
                f"{PLISIO_API_URL}/invoices/new",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            attempt.status = PaymentAttemptStatus.FAILED
            attempt.error_message = str(exc)
            attempt.save()
            payment_failed.send(sender=PaymentAttempt, attempt=attempt, invoice=invoice)
            raise

        attempt.response_payload = data
        if data.get("status") != "success":
            attempt.status = PaymentAttemptStatus.FAILED
            attempt.error_message = data.get("data", {}).get("message", "Plisio error")
            attempt.save()
            payment_failed.send(sender=PaymentAttempt, attempt=attempt, invoice=invoice)
            raise ValueError(attempt.error_message)

        invoice_data = data.get("data", {})
        txn_id = invoice_data.get("txn_id", "")
        invoice_url = invoice_data.get("invoice_url", "")

        attempt.status = PaymentAttemptStatus.PENDING
        attempt.external_id = txn_id
        attempt.payment_url = invoice_url
        attempt.save()

        invoice.payment_url = invoice_url
        invoice.external_id = txn_id
        invoice.save(update_fields=["payment_url", "external_id", "updated_at"])

        return attempt

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        data = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
        if isinstance(data, bytes):
            data = json.loads(data.decode())
        if isinstance(payload, bytes) and not isinstance(data, dict):
            data = json.loads(payload.decode())

        verify_hash = data.pop("verify_hash", None)
        secret = PackageSettings.plisio_callback_secret()
        if secret and verify_hash:
            ordered = sorted(data.items())
            check_string = urlencode(ordered) + secret
            expected = hashlib.sha1(check_string.encode()).hexdigest()  # noqa: S324
            if expected != verify_hash:
                raise ValueError("Invalid Plisio callback signature")
        return data

    def handle_webhook_event(self, event_data: dict) -> None:
        txn_id = event_data.get("txn_id", event_data.get("id", ""))
        status = event_data.get("status", "")
        order_number = event_data.get("order_number", "")

        idempotency_key = f"plisio:{txn_id}:{status}"
        webhook, created = WebhookEvent.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "provider": self.provider,
                "event_type": status,
                "payload": event_data,
            },
        )
        if not created and webhook.status == WebhookProcessingStatus.PROCESSED:
            return

        try:
            if status in ("completed", "confirmed", "mismatch"):
                if status in ("completed", "confirmed"):
                    self._handle_paid(event_data, order_number, txn_id)
                webhook.status = WebhookProcessingStatus.PROCESSED
            else:
                webhook.status = WebhookProcessingStatus.SKIPPED
            webhook.processed_at = timezone.now()
        except Exception as exc:
            webhook.status = WebhookProcessingStatus.FAILED
            webhook.error_message = str(exc)
            raise
        finally:
            webhook.save()

    def _handle_paid(self, event_data: dict, order_number: str, txn_id: str) -> None:
        if not order_number:
            return
        try:
            invoice = Invoice.objects.get(pk=int(order_number))
        except (Invoice.DoesNotExist, ValueError):
            return

        attempt = PaymentAttempt.objects.filter(
            invoice=invoice,
            provider=self.provider,
        ).order_by("-created_at").first()

        if attempt:
            attempt.status = PaymentAttemptStatus.SUCCEEDED
            attempt.external_id = txn_id
            attempt.response_payload = event_data
            attempt.save()

        ProviderTransaction.objects.get_or_create(
            provider=self.provider,
            external_id=txn_id,
            defaults={
                "invoice": invoice,
                "payment_attempt": attempt,
                "amount_minor": invoice.total_minor,
                "currency": invoice.currency,
                "status": "completed",
                "raw_payload": event_data,
            },
        )

        mark_invoice_paid(invoice, external_id=txn_id)

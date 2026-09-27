"""Plisio crypto invoices и callbacks."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from django_stripe_plisio.billing.enums import InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Invoice
from django_stripe_plisio.billing.money import minor_to_major_amount
from django_stripe_plisio.billing.services import mark_invoice_paid
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.exceptions import WebhookVerificationError
from django_stripe_plisio.payments.enums import PaymentAttemptStatus, WebhookProcessingStatus
from django_stripe_plisio.payments.models import PaymentAttempt, ProviderTransaction
from django_stripe_plisio.payments.services.base import BasePaymentProvider
from django_stripe_plisio.payments.sync_types import InvoiceSyncOutcome
from django_stripe_plisio.signals import payment_mismatch, send_on_commit

logger = logging.getLogger(__name__)

PLISIO_API_URL = "https://api.plisio.net/api/v1"

PAID_STATUSES = frozenset({"completed", "confirmed"})
CANCELLED_STATUSES = frozenset({"cancelled"})
# Клиент сменил криптовалюту: оплата придёт по новому txn с тем же order_number
DUPLICATE_STATUS = "cancelled duplicate"


class PlisioAPIError(Exception):
    """Ошибка Plisio API; текст не содержит api_key."""


def _is_positive_amount(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, ValueError):
        return False


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _with_json_flag(url: str) -> str:
    """Добавить ``json=true``: Plisio пришлёт JSON-callback, подписанный HMAC от JSON."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["json"] = "true"
    return urlunsplit(parts._replace(query=urlencode(query)))


def _js_compatible(value: Any) -> Any:
    """Привести значения к виду JSON.stringify (целые float → int)."""
    if isinstance(value, dict):
        return {k: _js_compatible(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_js_compatible(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def json_signature_base(payload: dict[str, Any]) -> str:
    """Строка подписи JSON-callback: JSON.stringify без verify_hash в исходном порядке ключей."""
    return json.dumps(_js_compatible(payload), ensure_ascii=False, separators=(",", ":"))


def _php_string(value: str) -> str:
    return f's:{len(value.encode())}:"{value}";'


def php_signature_base(payload: dict[str, Any]) -> str:
    """Строка подписи form-callback: PHP serialize(ksort($_POST)) без verify_hash."""
    items = []
    for key in sorted(payload):
        value = str(payload[key])
        if key == "tx_urls":
            value = html.unescape(value)
        items.append(_php_string(str(key)) + _php_string(value))
    return f"a:{len(items)}:{{{''.join(items)}}}"


class PlisioPaymentService(BasePaymentProvider):
    provider = PaymentProvider.PLISIO

    def _api_key(self) -> str:
        return PackageSettings.plisio_api_key()

    def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET к Plisio API → ``data`` из ответа; ошибки без api_key в тексте."""
        api_key = str(params.get("api_key", ""))
        try:
            response = requests.get(f"{PLISIO_API_URL}{path}", params=params, timeout=30)
        except requests.RequestException as exc:
            # from None: цепочка исключений содержит URL с api_key
            raise PlisioAPIError(_redact(f"{type(exc).__name__}: {exc}", api_key)) from None

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise PlisioAPIError(f"HTTP {response.status_code}: invalid JSON response")

        data = body.get("data")
        data = data if isinstance(data, dict) else {}
        if body.get("status") != "success":
            message = data.get("message") or f"HTTP {response.status_code}"
            raise PlisioAPIError(_redact(str(message), api_key))
        return data

    def create_checkout(self, invoice: Invoice) -> PaymentAttempt:
        callback_url = PackageSettings.plisio_webhook_url()
        if not callback_url:
            raise ImproperlyConfigured("DJANGO_STRIPE_PLISIO_PLISIO_WEBHOOK_URL is not configured")
        api_key = self._api_key()
        if not api_key:
            raise ImproperlyConfigured("DJANGO_STRIPE_PLISIO_PLISIO_API_KEY is not configured")

        # Plisio требует уникальный order_number: повторно отдаём уже созданный инвойс
        existing = self._latest_attempt(invoice, statuses=[PaymentAttemptStatus.PENDING])
        if existing is not None and existing.payment_url:
            return existing

        params: dict[str, Any] = {
            "api_key": api_key,
            "source_currency": invoice.currency,
            "source_amount": minor_to_major_amount(invoice.total_minor, invoice.currency),
            "order_number": str(invoice.pk),
            "order_name": f"Invoice {invoice.pk}",
            "callback_url": _with_json_flag(callback_url),
            "return_existing": "true",
        }
        success_url = PackageSettings.success_url()
        if success_url:
            params["success_invoice_url"] = success_url
        cancel_url = PackageSettings.cancel_url()
        if cancel_url:
            params["fail_invoice_url"] = cancel_url
        if invoice.expires_at:
            minutes_left = int((invoice.expires_at - timezone.now()).total_seconds() // 60)
            params["expire_min"] = max(1, minutes_left)

        attempt = PaymentAttempt.objects.create(
            invoice=invoice,
            provider=self.provider,
            status=PaymentAttemptStatus.CREATED,
            request_payload={k: v for k, v in params.items() if k != "api_key"},
        )

        try:
            invoice_data = self._api_get("/invoices/new", params)
        except PlisioAPIError as exc:
            return self._fail_attempt(attempt, str(exc))

        txn_id = str(invoice_data.get("txn_id", ""))
        invoice_url = str(invoice_data.get("invoice_url", ""))
        attempt.response_payload = invoice_data
        if not txn_id or not invoice_url:
            return self._fail_attempt(attempt, "Plisio response without txn_id/invoice_url")

        attempt.status = PaymentAttemptStatus.PENDING
        attempt.external_id = txn_id
        attempt.payment_url = invoice_url
        attempt.save()

        invoice.payment_url = invoice_url
        invoice.external_id = txn_id
        invoice.save(update_fields=["payment_url", "external_id", "updated_at"])
        return attempt

    @staticmethod
    def _fail_attempt(attempt: PaymentAttempt, message: str) -> PaymentAttempt:
        attempt.status = PaymentAttemptStatus.FAILED
        attempt.error_message = message
        attempt.save()
        return attempt

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        """Проверка JSON-callback (``callback_url`` с ``json=true``)."""
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookVerificationError("Invalid Plisio callback payload") from exc
        if not isinstance(data, dict):
            raise WebhookVerificationError("Invalid Plisio callback payload")
        return self._verify_plisio_data(data)

    def verify_webhook_from_post(self, post_data: dict[str, Any]) -> dict:
        """Проверка callback из application/x-www-form-urlencoded (без ``json=true``)."""
        return self._verify_plisio_data(dict(post_data), form_encoded=True)

    def _verify_plisio_data(
        self,
        data: dict[str, Any],
        *,
        form_encoded: bool = False,
    ) -> dict[str, Any]:
        payload = dict(data)
        verify_hash = payload.pop("verify_hash", None)
        secret = PackageSettings.plisio_callback_secret()

        if not secret:
            if PackageSettings.require_webhook_secret():
                raise WebhookVerificationError("PLISIO_CALLBACK_SECRET is not configured")
            logger.warning("Plisio callback accepted WITHOUT signature check (no secret)")
            return payload

        # Секрет задан — подпись обязательна независимо от REQUIRE_WEBHOOK_SECRET
        if not verify_hash:
            raise WebhookVerificationError("Missing Plisio verify_hash")

        base = php_signature_base(payload) if form_encoded else json_signature_base(payload)
        expected = hmac.new(secret.encode(), base.encode(), hashlib.sha1).hexdigest()
        if not hmac.compare_digest(expected, str(verify_hash)):
            raise WebhookVerificationError("Invalid Plisio callback signature")
        return payload

    def handle_webhook_event(self, event_data: dict) -> None:
        txn_id = str(event_data.get("txn_id") or event_data.get("id") or "")
        status = str(event_data.get("status", ""))
        order_number = str(event_data.get("order_number", ""))

        self._process_webhook(
            idempotency_key=f"plisio:{txn_id}:{status}",
            event_type=status,
            payload=event_data,
            handler=lambda: self._dispatch_status(event_data, status, order_number, txn_id),
        )

    def _dispatch_status(
        self,
        event_data: dict[str, Any],
        status: str,
        order_number: str,
        txn_id: str,
    ) -> str:
        if status in PAID_STATUSES:
            self._handle_paid(event_data, order_number, txn_id)
            return WebhookProcessingStatus.PROCESSED

        if status in ("mismatch", "expired", *CANCELLED_STATUSES):
            invoice = self._find_invoice(order_number)
            if invoice is None:
                return WebhookProcessingStatus.SKIPPED
            if status == "mismatch":
                self._notify_mismatch(invoice, event_data, reason="mismatch")
            elif status == "expired":
                # Plisio: expired может означать частичную оплату — нужна ручная проверка
                if _is_positive_amount(event_data.get("amount")):
                    self._notify_mismatch(invoice, event_data, reason="expired_partial")
                self._apply_remote_unpaid_status(
                    invoice,
                    invoice_status=InvoiceStatus.EXPIRED,
                    attempt_status=PaymentAttemptStatus.FAILED,
                )
            else:
                self._apply_remote_unpaid_status(
                    invoice,
                    invoice_status=InvoiceStatus.CANCELLED,
                    attempt_status=PaymentAttemptStatus.CANCELLED,
                )
            return WebhookProcessingStatus.PROCESSED

        return WebhookProcessingStatus.SKIPPED

    def _notify_mismatch(self, invoice: Invoice, payload: dict[str, Any], *, reason: str) -> None:
        logger.warning("Plisio %s for invoice %s, payment not applied", reason, invoice.pk)
        send_on_commit(
            payment_mismatch,
            sender=Invoice,
            invoice=invoice,
            provider=self.provider,
            payload=payload,
            reason=reason,
        )

    def _handle_paid(self, event_data: dict, order_number: str, txn_id: str) -> bool:
        invoice = self._find_invoice(order_number)
        if invoice is None:
            return False

        source_currency = str(event_data.get("source_currency") or "").upper()
        if source_currency and source_currency != invoice.currency:
            self._notify_mismatch(invoice, event_data, reason="currency_mismatch")
            return False

        attempt = self._latest_attempt(invoice)
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
        return True

    @transaction.atomic
    def _apply_remote_unpaid_status(
        self,
        invoice: Invoice,
        *,
        invoice_status: str,
        attempt_status: str,
    ) -> InvoiceSyncOutcome:
        # Блокировка: параллельный webhook мог уже отметить счёт оплаченным
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status != InvoiceStatus.PENDING:
            return InvoiceSyncOutcome.UNCHANGED

        invoice.status = invoice_status
        invoice.save(update_fields=["status", "updated_at"])

        attempt = self._latest_attempt(invoice)
        if attempt:
            attempt.status = attempt_status
            attempt.save(update_fields=["status", "updated_at"])

        if invoice_status == InvoiceStatus.EXPIRED:
            return InvoiceSyncOutcome.EXPIRED
        return InvoiceSyncOutcome.CANCELLED

    def sync_invoice_status(self, invoice: Invoice) -> InvoiceSyncOutcome:
        """Опрос Plisio operation по txn_id (external_id счёта)."""
        if invoice.status != InvoiceStatus.PENDING:
            return InvoiceSyncOutcome.UNCHANGED

        txn_id = invoice.external_id
        if not txn_id:
            return InvoiceSyncOutcome.UNCHANGED

        api_key = self._api_key()
        if not api_key:
            logger.warning("Plisio API key not configured, skip sync for invoice %s", invoice.pk)
            return InvoiceSyncOutcome.ERROR

        try:
            operation = self._api_get(f"/operations/{txn_id}", {"api_key": api_key})
        except PlisioAPIError as exc:
            logger.error("Plisio operation retrieve failed for invoice %s: %s", invoice.pk, exc)
            return InvoiceSyncOutcome.ERROR

        remote_order = str(operation.get("order_number") or invoice.pk)
        if remote_order != str(invoice.pk):
            logger.error(
                "Plisio txn %s belongs to order %s, not invoice %s",
                txn_id,
                remote_order,
                invoice.pk,
            )
            return InvoiceSyncOutcome.ERROR

        remote_status = str(operation.get("status", ""))

        if remote_status in PAID_STATUSES:
            if self._handle_paid(operation, str(invoice.pk), txn_id):
                return InvoiceSyncOutcome.PAID
            return InvoiceSyncOutcome.SKIPPED

        if remote_status in ("mismatch", DUPLICATE_STATUS):
            logger.warning(
                "Plisio %s for invoice %s (txn_id=%s), payment not applied",
                remote_status,
                invoice.pk,
                txn_id,
            )
            return InvoiceSyncOutcome.SKIPPED

        if remote_status == "expired":
            return self._apply_remote_unpaid_status(
                invoice,
                invoice_status=InvoiceStatus.EXPIRED,
                attempt_status=PaymentAttemptStatus.FAILED,
            )

        if remote_status in CANCELLED_STATUSES:
            return self._apply_remote_unpaid_status(
                invoice,
                invoice_status=InvoiceStatus.CANCELLED,
                attempt_status=PaymentAttemptStatus.CANCELLED,
            )

        return InvoiceSyncOutcome.UNCHANGED

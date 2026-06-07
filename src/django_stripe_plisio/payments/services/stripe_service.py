"""Stripe Checkout и webhooks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import stripe
from django.db import transaction
from django.utils import timezone

from django_stripe_plisio.billing.enums import BillingPeriod, InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Invoice, InvoiceLine
from django_stripe_plisio.billing.services import mark_invoice_paid
from django_stripe_plisio.billing.utils import user_external_id
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.exceptions import WebhookVerificationError
from django_stripe_plisio.payments.enums import (
    PaymentAttemptStatus,
    StripeSubscriptionStatus,
    WebhookProcessingStatus,
)
from django_stripe_plisio.payments.models import (
    PaymentAttempt,
    ProviderTransaction,
    StripeSubscription,
    WebhookEvent,
)
from django_stripe_plisio.payments.services.base import BasePaymentProvider
from django_stripe_plisio.payments.sync_types import InvoiceSyncOutcome
from django_stripe_plisio.signals import payment_failed

logger = logging.getLogger(__name__)


class StripePaymentService(BasePaymentProvider):
    provider = "stripe"

    def __init__(self) -> None:
        stripe.api_key = PackageSettings.stripe_secret_key()

    def _stripe_metadata(self, invoice: Invoice) -> dict[str, str]:
        return {
            "invoice_id": str(invoice.pk),
            "user_id": user_external_id(invoice.user),
        }

    def create_checkout(self, invoice: Invoice) -> PaymentAttempt:
        attempt = PaymentAttempt.objects.create(
            invoice=invoice,
            provider=self.provider,
            status=PaymentAttemptStatus.CREATED,
        )

        line = InvoiceLine.objects.filter(invoice=invoice).select_related("price").first()
        mode = "payment"
        if line and line.price and line.price.billing_period != BillingPeriod.ONE_TIME:
            mode = "subscription"

        success_url = PackageSettings.success_url() or "https://example.com/success"
        cancel_url = PackageSettings.cancel_url() or "https://example.com/cancel"

        metadata = self._stripe_metadata(invoice)
        session_params: dict[str, Any] = {
            "mode": mode,
            "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "client_reference_id": str(invoice.pk),
            "metadata": metadata,
            "line_items": self._build_line_items(invoice, line, mode),
        }

        try:
            session = stripe.checkout.Session.create(**session_params)
        except stripe.StripeError as exc:
            attempt.status = PaymentAttemptStatus.FAILED
            attempt.error_code = type(exc).__name__
            attempt.error_message = str(exc)
            attempt.save()
            payment_failed.send(sender=PaymentAttempt, attempt=attempt, invoice=invoice)
            raise

        attempt.status = PaymentAttemptStatus.PENDING
        attempt.external_id = session.id
        attempt.payment_url = session.url or ""
        attempt.response_payload = {"id": session.id, "url": session.url}
        attempt.save()

        invoice.payment_url = attempt.payment_url
        invoice.external_id = session.id
        invoice.save(update_fields=["payment_url", "external_id", "updated_at"])

        return attempt

    def _build_line_items(
        self,
        invoice: Invoice,
        line: InvoiceLine | None,
        mode: str,
    ) -> list[dict[str, Any]]:
        if line and line.price and line.price.stripe_price_id:
            return [{"price": line.price.stripe_price_id, "quantity": 1}]

        name = line.description if line else f"Invoice {invoice.pk}"
        price_data: dict[str, Any] = {
            "currency": invoice.currency.lower(),
            "unit_amount": invoice.total_minor,
            "product_data": {"name": name},
        }

        if mode == "subscription" and line and line.price:
            interval = (
                "year" if line.price.billing_period == BillingPeriod.YEAR else "month"
            )
            price_data["recurring"] = {"interval": interval}

        return [{"price_data": price_data, "quantity": 1}]

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        secret = PackageSettings.stripe_webhook_secret()
        if PackageSettings.require_webhook_secret() and not secret:
            raise WebhookVerificationError("STRIPE_WEBHOOK_SECRET is not configured")

        if not secret:
            raise WebhookVerificationError("STRIPE_WEBHOOK_SECRET is required")

        sig = headers.get("Stripe-Signature", headers.get("stripe-signature", ""))
        try:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise WebhookVerificationError("Invalid Stripe webhook signature") from exc
        return dict(event)

    @transaction.atomic
    def handle_webhook_event(self, event_data: dict) -> None:
        event_id = event_data.get("id", "")
        event_type = event_data.get("type", "")

        webhook, created = WebhookEvent.objects.get_or_create(
            idempotency_key=f"stripe:{event_id}",
            defaults={
                "provider": self.provider,
                "event_type": event_type,
                "payload": event_data,
            },
        )
        if not created:
            webhook = WebhookEvent.objects.select_for_update().get(pk=webhook.pk)
            if webhook.status == WebhookProcessingStatus.PROCESSED:
                return

        try:
            if event_type == "checkout.session.completed":
                self._handle_checkout_completed(event_data)
            elif event_type in (
                "customer.subscription.created",
                "customer.subscription.updated",
            ):
                self._handle_subscription_upsert(event_data)
            elif event_type == "customer.subscription.deleted":
                self._handle_subscription_deleted(event_data)
            webhook.status = WebhookProcessingStatus.PROCESSED
            webhook.processed_at = timezone.now()
        except Exception as exc:
            webhook.status = WebhookProcessingStatus.FAILED
            webhook.error_message = str(exc)
            logger.exception("Stripe webhook processing failed")
            raise
        finally:
            webhook.save()

    def _handle_checkout_completed(self, event_data: dict) -> None:
        obj = event_data.get("data", {}).get("object", {})
        self._apply_checkout_session_paid(obj)

    def _apply_checkout_session_paid(self, obj: dict) -> bool:
        """Зафиксировать оплату по объекту Checkout Session. Возвращает True, если счёт оплачен."""
        invoice_id = obj.get("metadata", {}).get("invoice_id") or obj.get("client_reference_id")
        if not invoice_id:
            return False

        try:
            invoice = Invoice.objects.get(pk=int(invoice_id))
        except (Invoice.DoesNotExist, ValueError):
            return False

        session_id = obj.get("id", "")
        attempt = PaymentAttempt.objects.filter(
            invoice=invoice,
            provider=self.provider,
            external_id=session_id,
        ).first()

        if attempt:
            attempt.status = PaymentAttemptStatus.SUCCEEDED
            attempt.response_payload = obj
            attempt.save()

        ProviderTransaction.objects.get_or_create(
            provider=self.provider,
            external_id=session_id or f"inv-{invoice.pk}",
            defaults={
                "invoice": invoice,
                "payment_attempt": attempt,
                "amount_minor": invoice.total_minor,
                "currency": invoice.currency,
                "status": "completed",
                "raw_payload": obj,
            },
        )

        mark_invoice_paid(invoice, external_id=session_id)

        subscription_id = obj.get("subscription")
        if subscription_id:
            self._upsert_subscription_from_stripe(
                invoice=invoice,
                stripe_subscription_id=subscription_id,
                stripe_customer_id=obj.get("customer", ""),
            )
        return True

    def sync_invoice_status(self, invoice: Invoice) -> InvoiceSyncOutcome:
        """Опрос Stripe Checkout Session по external_id счёта."""
        if invoice.status != InvoiceStatus.PENDING:
            return InvoiceSyncOutcome.UNCHANGED

        session_id = invoice.external_id
        if not session_id:
            return InvoiceSyncOutcome.UNCHANGED

        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except stripe.StripeError:
            logger.exception("Stripe session retrieve failed for invoice %s", invoice.pk)
            return InvoiceSyncOutcome.ERROR

        obj = dict(session)
        payment_status = obj.get("payment_status", "")
        status = obj.get("status", "")

        if payment_status == "paid" and status == "complete":
            self._apply_checkout_session_paid(obj)
            return InvoiceSyncOutcome.PAID

        return InvoiceSyncOutcome.UNCHANGED

    def _handle_subscription_upsert(self, event_data: dict) -> None:
        obj = event_data.get("data", {}).get("object", {})
        sub_id = obj.get("id")
        if not sub_id:
            return

        metadata = obj.get("metadata") or {}
        invoice_id = metadata.get("invoice_id")
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(pk=int(invoice_id))
            except (Invoice.DoesNotExist, ValueError):
                invoice = None

        self._upsert_subscription_from_stripe(
            invoice=invoice,
            stripe_subscription_id=sub_id,
            stripe_customer_id=obj.get("customer", ""),
            status=obj.get("status", StripeSubscriptionStatus.INCOMPLETE),
            current_period_end=self._stripe_timestamp(obj.get("current_period_end")),
            raw=obj,
        )

    def _handle_subscription_deleted(self, event_data: dict) -> None:
        obj = event_data.get("data", {}).get("object", {})
        sub_id = obj.get("id")
        if not sub_id:
            return
        StripeSubscription.objects.filter(stripe_subscription_id=sub_id).update(
            status=StripeSubscriptionStatus.CANCELED,
            updated_at=timezone.now(),
        )

    def _upsert_subscription_from_stripe(
        self,
        *,
        invoice: Invoice | None,
        stripe_subscription_id: str,
        stripe_customer_id: str,
        status: str = "active",
        current_period_end: datetime | None = None,
        raw: dict | None = None,
    ) -> None:
        user = invoice.user if invoice else None
        price = None
        if invoice:
            line = InvoiceLine.objects.filter(invoice=invoice).select_related("price").first()
            price = line.price if line else None

        if user is None:
            return

        StripeSubscription.objects.update_or_create(
            stripe_subscription_id=stripe_subscription_id,
            defaults={
                "user": user,
                "price": price,
                "stripe_customer_id": stripe_customer_id or "",
                "status": status,
                "current_period_end": current_period_end,
                "metadata": raw or {},
            },
        )

    @staticmethod
    def _stripe_timestamp(value: int | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromtimestamp(value, tz=UTC)

"""Stripe Checkout и webhooks."""

from __future__ import annotations

import json
from typing import Any

import stripe
from django.utils import timezone

from django_stripe_plisio.billing.enums import BillingPeriod, PaymentProvider
from django_stripe_plisio.billing.models import Invoice, InvoiceLine
from django_stripe_plisio.billing.services import mark_invoice_paid
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.payments.enums import PaymentAttemptStatus, WebhookProcessingStatus
from django_stripe_plisio.payments.models import PaymentAttempt, ProviderTransaction, WebhookEvent
from django_stripe_plisio.payments.services.base import BasePaymentProvider
from django_stripe_plisio.signals import payment_failed


class StripePaymentService(BasePaymentProvider):
    provider = PaymentProvider.STRIPE

    def __init__(self) -> None:
        stripe.api_key = PackageSettings.stripe_secret_key()

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

        session_params: dict[str, Any] = {
            "mode": mode,
            "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "client_reference_id": str(invoice.pk),
            "metadata": {"invoice_id": str(invoice.pk)},
            "line_items": [
                {
                    "price_data": {
                        "currency": invoice.currency.lower(),
                        "unit_amount": invoice.total_minor,
                        "product_data": {
                            "name": line.description if line else f"Invoice {invoice.pk}",
                        },
                    },
                    "quantity": 1,
                },
            ],
        }

        if line and line.price and line.price.stripe_price_id:
            session_params["line_items"] = [{"price": line.price.stripe_price_id, "quantity": 1}]

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

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        sig = headers.get("Stripe-Signature", headers.get("stripe-signature", ""))
        secret = PackageSettings.stripe_webhook_secret()
        if not secret:
            return json.loads(payload)
        event = stripe.Webhook.construct_event(payload, sig, secret)
        return dict(event)

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
        if not created and webhook.status == WebhookProcessingStatus.PROCESSED:
            return

        try:
            if event_type == "checkout.session.completed":
                self._handle_checkout_completed(event_data)
            webhook.status = WebhookProcessingStatus.PROCESSED
            webhook.processed_at = timezone.now()
        except Exception as exc:
            webhook.status = WebhookProcessingStatus.FAILED
            webhook.error_message = str(exc)
            raise
        finally:
            webhook.save()

    def _handle_checkout_completed(self, event_data: dict) -> None:
        obj = event_data.get("data", {}).get("object", {})
        invoice_id = obj.get("metadata", {}).get("invoice_id") or obj.get("client_reference_id")
        if not invoice_id:
            return

        try:
            invoice = Invoice.objects.get(pk=int(invoice_id))
        except (Invoice.DoesNotExist, ValueError):
            return

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

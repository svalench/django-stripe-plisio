"""Stripe Checkout и webhooks."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import stripe
from django.utils import timezone

from django_stripe_plisio.billing.enums import BillingPeriod, InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Invoice, InvoiceLine, UserEntitlement
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
)
from django_stripe_plisio.payments.services.base import BasePaymentProvider
from django_stripe_plisio.payments.sync_types import InvoiceSyncOutcome
from django_stripe_plisio.signals import (
    payment_failed,
    payment_mismatch,
    send_on_commit,
    subscription_changed,
)

logger = logging.getLogger(__name__)

PAID_SESSION_STATUSES = frozenset({"paid", "no_payment_required"})
# Пока подписка в этих статусах — доступ до current_period_end
ACCESS_SUBSCRIPTION_STATUSES = frozenset(
    {
        StripeSubscriptionStatus.ACTIVE,
        StripeSubscriptionStatus.TRIALING,
        StripeSubscriptionStatus.PAST_DUE,
    }
)
REVOKE_SUBSCRIPTION_STATUSES = frozenset(
    {
        StripeSubscriptionStatus.CANCELED,
        StripeSubscriptionStatus.UNPAID,
        StripeSubscriptionStatus.INCOMPLETE_EXPIRED,
        StripeSubscriptionStatus.PAUSED,
    }
)
# Stripe: expires_at сессии — от 30 минут до 24 часов с момента создания
SESSION_MIN_TTL = timedelta(minutes=31)
SESSION_MAX_TTL = timedelta(hours=23, minutes=59)


def _as_dict(obj: Any) -> dict[str, Any]:
    """StripeObject → обычный dict (JSON-совместимый для JSONField)."""
    if type(obj) is dict:
        return obj
    return json.loads(json.dumps(obj.to_dict()))


class StripePaymentService(BasePaymentProvider):
    provider = PaymentProvider.STRIPE

    def __init__(self) -> None:
        # Ключ передаётся в каждый вызов, глобальный stripe.api_key не трогаем
        self._api_key = PackageSettings.stripe_secret_key()

    def _stripe_metadata(self, invoice: Invoice) -> dict[str, str]:
        return {
            "invoice_id": str(invoice.pk),
            "user_id": user_external_id(invoice.user),
        }

    @staticmethod
    def _first_line(invoice: Invoice) -> InvoiceLine | None:
        return InvoiceLine.objects.filter(invoice=invoice).select_related("price").first()

    @staticmethod
    def _checkout_mode(line: InvoiceLine | None) -> str:
        if line and line.price and line.price.billing_period != BillingPeriod.ONE_TIME:
            return "subscription"
        return "payment"

    def create_checkout(self, invoice: Invoice) -> PaymentAttempt:
        reused = self._reuse_open_session(invoice)
        if reused is not None:
            return reused

        attempt = PaymentAttempt.objects.create(
            invoice=invoice,
            provider=self.provider,
            status=PaymentAttemptStatus.CREATED,
        )

        try:
            session_params = self._session_params(invoice)
            attempt.request_payload = session_params
            session = stripe.checkout.Session.create(api_key=self._api_key, **session_params)
        except stripe.StripeError as exc:
            attempt.status = PaymentAttemptStatus.FAILED
            attempt.error_code = type(exc).__name__
            attempt.error_message = str(exc)
            attempt.save()
            return attempt

        attempt.status = PaymentAttemptStatus.PENDING
        attempt.external_id = session.id
        attempt.payment_url = session.url or ""
        attempt.response_payload = {"id": session.id, "url": session.url}
        attempt.save()

        invoice.payment_url = attempt.payment_url
        invoice.external_id = session.id
        invoice.save(update_fields=["payment_url", "external_id", "updated_at"])

        return attempt

    def _reuse_open_session(self, invoice: Invoice) -> PaymentAttempt | None:
        """Одна открытая сессия на счёт: иначе клиент может оплатить обе."""
        previous = self._latest_attempt(invoice, statuses=[PaymentAttemptStatus.PENDING])
        if previous is None or not previous.external_id:
            return None

        try:
            session = stripe.checkout.Session.retrieve(previous.external_id, api_key=self._api_key)
        except stripe.StripeError:
            # Статус старой сессии неизвестен — новую не создаём, чтобы не допустить двойной оплаты
            logger.warning("Stripe session %s retrieve failed, reuse it", previous.external_id)
            return previous

        obj = _as_dict(session)
        status = obj.get("status")
        if status == "open":
            return previous
        if status == "complete":
            self._apply_checkout_session_paid(obj)
            previous.refresh_from_db()
            return previous

        previous.status = PaymentAttemptStatus.CANCELLED
        previous.save(update_fields=["status", "updated_at"])
        return None

    def _session_params(self, invoice: Invoice) -> dict[str, Any]:
        line = self._first_line(invoice)
        mode = self._checkout_mode(line)
        success_url = PackageSettings.success_url() or "https://example.com/success"
        cancel_url = PackageSettings.cancel_url() or "https://example.com/cancel"
        metadata = self._stripe_metadata(invoice)

        params: dict[str, Any] = {
            "mode": mode,
            "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "client_reference_id": str(invoice.pk),
            "metadata": metadata,
            "line_items": self._build_line_items(invoice, line, mode),
        }

        if mode == "subscription":
            # Без metadata в subscription_data события customer.subscription.* не связать со счётом
            params["subscription_data"] = {"metadata": metadata}
            if invoice.discount_minor > 0:
                params["discounts"] = [{"coupon": self._create_once_coupon(invoice)}]
        else:
            params["payment_intent_data"] = {"metadata": metadata}

        if invoice.expires_at:
            ttl = invoice.expires_at - timezone.now()
            if SESSION_MIN_TTL <= ttl <= SESSION_MAX_TTL:
                params["expires_at"] = int(invoice.expires_at.timestamp())

        return params

    def _build_line_items(
        self,
        invoice: Invoice,
        line: InvoiceLine | None,
        mode: str,
    ) -> list[dict[str, Any]]:
        if mode == "subscription" and line and line.price:
            # Скидка подписки — разовым купоном, чтобы не снижать цену всех следующих периодов
            if line.price.stripe_price_id:
                return [{"price": line.price.stripe_price_id, "quantity": line.quantity}]
            interval = "year" if line.price.billing_period == BillingPeriod.YEAR else "month"
            return [
                {
                    "price_data": {
                        "currency": invoice.currency.lower(),
                        "unit_amount": line.unit_amount_minor,
                        "product_data": {"name": line.description},
                        "recurring": {"interval": interval},
                    },
                    "quantity": line.quantity,
                }
            ]

        if line and line.price and line.price.stripe_price_id and invoice.discount_minor == 0:
            return [{"price": line.price.stripe_price_id, "quantity": line.quantity}]

        # Разовая оплата со скидкой или без Stripe Price — одна позиция на итог счёта
        name = line.description if line else f"Invoice {invoice.pk}"
        if line and line.quantity > 1:
            name = f"{name} x{line.quantity}"
        return [
            {
                "price_data": {
                    "currency": invoice.currency.lower(),
                    "unit_amount": invoice.total_minor,
                    "product_data": {"name": name},
                },
                "quantity": 1,
            }
        ]

    def _create_once_coupon(self, invoice: Invoice) -> str:
        coupon = stripe.Coupon.create(
            api_key=self._api_key,
            amount_off=invoice.discount_minor,
            currency=invoice.currency.lower(),
            duration="once",
            max_redemptions=1,
            name=f"Invoice {invoice.pk} discount",
            metadata={"invoice_id": str(invoice.pk)},
        )
        return coupon.id

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> dict:
        secret = PackageSettings.stripe_webhook_secret()
        if not secret:
            raise WebhookVerificationError("STRIPE_WEBHOOK_SECRET is not configured")

        sig = headers.get("Stripe-Signature", headers.get("stripe-signature", ""))
        try:
            body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            # Только подпись: разбор события SDK (construct_event) падает на нестандартных
            # payload; tolerance обязателен — по умолчанию verify_header не проверяет время
            stripe.WebhookSignature.verify_header(
                body,
                sig,
                secret,
                tolerance=stripe.Webhook.DEFAULT_TOLERANCE,
            )
            event = json.loads(body)
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise WebhookVerificationError("Invalid Stripe webhook signature") from exc
        if not isinstance(event, dict):
            raise WebhookVerificationError("Invalid Stripe webhook payload")
        return event

    def handle_webhook_event(self, event_data: dict) -> None:
        event_id = event_data.get("id", "")
        event_type = event_data.get("type", "")
        obj = (event_data.get("data") or {}).get("object") or {}

        self._process_webhook(
            idempotency_key=f"stripe:{event_id}",
            event_type=event_type,
            payload=event_data,
            handler=lambda: self._dispatch_event(event_type, obj),
        )

    def _dispatch_event(self, event_type: str, obj: dict[str, Any]) -> str:
        if event_type in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
        ):
            self._apply_checkout_session_paid(obj)
        elif event_type == "checkout.session.async_payment_failed":
            self._handle_async_payment_failed(obj)
        elif event_type == "checkout.session.expired":
            self._handle_session_expired(obj)
        elif event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
        ):
            self._handle_subscription_upsert(obj)
        elif event_type == "customer.subscription.deleted":
            self._handle_subscription_deleted(obj)
        else:
            return WebhookProcessingStatus.SKIPPED
        return WebhookProcessingStatus.PROCESSED

    def _invoice_from_session(self, obj: dict[str, Any]) -> Invoice | None:
        invoice_id = (obj.get("metadata") or {}).get("invoice_id") or obj.get("client_reference_id")
        if not invoice_id:
            return None
        return self._find_invoice(invoice_id)

    def _apply_checkout_session_paid(self, obj: dict) -> bool:
        """Зафиксировать оплату по объекту Checkout Session. Возвращает True, если счёт оплачен."""
        invoice = self._invoice_from_session(obj)
        if invoice is None:
            return False

        session_id = obj.get("id", "")
        if obj.get("payment_status") not in PAID_SESSION_STATUSES:
            # Асинхронные методы: итог придёт в checkout.session.async_payment_*
            logger.info("Stripe session %s is not paid yet", session_id)
            return False

        amount_total = obj.get("amount_total")
        currency = str(obj.get("currency") or "").upper()
        if (
            amount_total is None
            or currency != invoice.currency
            or int(amount_total) < invoice.total_minor
        ):
            logger.error(
                "Stripe session %s amount %s %s does not match invoice %s (%s %s)",
                session_id,
                amount_total,
                currency,
                invoice.pk,
                invoice.total_minor,
                invoice.currency,
            )
            send_on_commit(
                payment_mismatch,
                sender=Invoice,
                invoice=invoice,
                provider=self.provider,
                payload=obj,
                reason="amount_mismatch",
            )
            return False

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
                "amount_minor": int(amount_total),
                "currency": currency,
                "status": "completed",
                "raw_payload": obj,
            },
        )

        mark_invoice_paid(invoice, external_id=session_id)

        subscription_id = obj.get("subscription")
        if subscription_id:
            self._link_subscription_to_invoice(
                invoice=invoice,
                stripe_subscription_id=subscription_id,
                stripe_customer_id=obj.get("customer") or "",
            )
        return True

    def _handle_async_payment_failed(self, obj: dict[str, Any]) -> None:
        invoice = self._invoice_from_session(obj)
        if invoice is None:
            return
        session_id = obj.get("id", "")
        attempt = PaymentAttempt.objects.filter(
            invoice=invoice,
            provider=self.provider,
            external_id=session_id,
        ).first()
        if attempt:
            attempt.status = PaymentAttemptStatus.FAILED
            attempt.error_message = "Async payment failed"
            attempt.response_payload = obj
            attempt.save()
        self._release_invoice_session(session_id)
        send_on_commit(payment_failed, sender=PaymentAttempt, attempt=attempt, invoice=invoice)

    def _handle_session_expired(self, obj: dict[str, Any]) -> None:
        session_id = obj.get("id", "")
        if not session_id:
            return
        PaymentAttempt.objects.filter(
            provider=self.provider,
            external_id=session_id,
            status__in=[PaymentAttemptStatus.CREATED, PaymentAttemptStatus.PENDING],
        ).update(status=PaymentAttemptStatus.CANCELLED, updated_at=timezone.now())
        self._release_invoice_session(session_id)

    @staticmethod
    def _release_invoice_session(session_id: str) -> None:
        """Отвязать мёртвую сессию: счёт снова можно оплатить новым checkout."""
        if not session_id:
            return
        Invoice.objects.filter(external_id=session_id, status=InvoiceStatus.PENDING).update(
            external_id="",
            payment_url="",
            updated_at=timezone.now(),
        )

    def sync_invoice_status(self, invoice: Invoice) -> InvoiceSyncOutcome:
        """Опрос Stripe Checkout Session по external_id счёта."""
        if invoice.status != InvoiceStatus.PENDING:
            return InvoiceSyncOutcome.UNCHANGED

        session_id = invoice.external_id
        if not session_id:
            return InvoiceSyncOutcome.UNCHANGED

        try:
            session = stripe.checkout.Session.retrieve(session_id, api_key=self._api_key)
        except stripe.StripeError as exc:
            logger.error("Stripe session retrieve failed for invoice %s: %s", invoice.pk, exc)
            return InvoiceSyncOutcome.ERROR

        obj = _as_dict(session)
        status = obj.get("status", "")

        if status == "complete" and obj.get("payment_status") in PAID_SESSION_STATUSES:
            if self._apply_checkout_session_paid(obj):
                return InvoiceSyncOutcome.PAID
            return InvoiceSyncOutcome.SKIPPED

        if status == "expired":
            self._handle_session_expired(obj)
            return InvoiceSyncOutcome.SKIPPED

        return InvoiceSyncOutcome.UNCHANGED

    @staticmethod
    def _subscription_period_end(obj: dict[str, Any]) -> datetime | None:
        # С API 2025-03-31 current_period_end перенесён в items.data[]
        value = obj.get("current_period_end")
        if not value:
            items = (obj.get("items") or {}).get("data") or []
            ends = [i["current_period_end"] for i in items if i.get("current_period_end")]
            value = max(ends) if ends else None
        if not value:
            return None
        return datetime.fromtimestamp(int(value), tz=UTC)

    def _link_subscription_to_invoice(
        self,
        *,
        invoice: Invoice,
        stripe_subscription_id: str,
        stripe_customer_id: str,
    ) -> None:
        """Связать подписку с оплаченным счётом и выданным доступом.

        Статус и период не трогаем: их источник — события customer.subscription.*,
        которые могут прийти раньше checkout.session.completed.
        """
        line = self._first_line(invoice)
        entitlement = UserEntitlement.objects.filter(invoice=invoice).order_by("-pk").first()
        subscription, created = StripeSubscription.objects.get_or_create(
            stripe_subscription_id=stripe_subscription_id,
            defaults={
                "user": invoice.user,
                "price": line.price if line else None,
                "stripe_customer_id": stripe_customer_id,
                "status": StripeSubscriptionStatus.ACTIVE,
                "entitlement": entitlement,
            },
        )
        if not created:
            subscription.user = invoice.user
            subscription.price = line.price if line else subscription.price
            subscription.stripe_customer_id = stripe_customer_id or subscription.stripe_customer_id
            subscription.entitlement = entitlement or subscription.entitlement
            subscription.save(
                update_fields=["user", "price", "stripe_customer_id", "entitlement", "updated_at"],
            )
        self._sync_entitlement_period(subscription)

    def _handle_subscription_upsert(self, obj: dict[str, Any]) -> None:
        sub_id = obj.get("id")
        if not sub_id:
            return

        subscription = StripeSubscription.objects.filter(stripe_subscription_id=sub_id).first()
        if subscription is None:
            invoice = self._invoice_from_session({"metadata": obj.get("metadata") or {}})
            if invoice is None:
                logger.info("Stripe subscription %s is not linked to an invoice", sub_id)
                return
            line = self._first_line(invoice)
            subscription, _ = StripeSubscription.objects.get_or_create(
                stripe_subscription_id=sub_id,
                defaults={
                    "user": invoice.user,
                    "price": line.price if line else None,
                    "entitlement": UserEntitlement.objects.filter(invoice=invoice)
                    .order_by("-pk")
                    .first(),
                },
            )

        subscription.status = obj.get("status") or StripeSubscriptionStatus.INCOMPLETE
        subscription.current_period_end = (
            self._subscription_period_end(obj) or subscription.current_period_end
        )
        subscription.stripe_customer_id = obj.get("customer") or subscription.stripe_customer_id
        subscription.metadata = obj
        subscription.save()

        self._sync_entitlement_period(subscription)
        send_on_commit(subscription_changed, sender=StripeSubscription, subscription=subscription)

    def _handle_subscription_deleted(self, obj: dict[str, Any]) -> None:
        sub_id = obj.get("id")
        if not sub_id:
            return
        subscription = StripeSubscription.objects.filter(stripe_subscription_id=sub_id).first()
        if subscription is None:
            return
        subscription.status = StripeSubscriptionStatus.CANCELED
        subscription.metadata = obj
        subscription.save(update_fields=["status", "metadata", "updated_at"])

        self._sync_entitlement_period(subscription)
        send_on_commit(subscription_changed, sender=StripeSubscription, subscription=subscription)

    @staticmethod
    def _sync_entitlement_period(subscription: StripeSubscription) -> None:
        """Продлить доступ до конца оплаченного периода или отозвать при отмене."""
        entitlement = subscription.entitlement
        if entitlement is None:
            return

        if subscription.status in ACCESS_SUBSCRIPTION_STATUSES:
            if subscription.current_period_end is None:
                return
            active_until = subscription.current_period_end
        elif subscription.status in REVOKE_SUBSCRIPTION_STATUSES:
            now = timezone.now()
            current = entitlement.active_until
            active_until = min(current, now) if current else now
        else:
            return

        if entitlement.active_until != active_until:
            entitlement.active_until = active_until
            entitlement.save(update_fields=["active_until"])

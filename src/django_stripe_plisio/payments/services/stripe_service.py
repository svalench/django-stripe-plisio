"""Stripe Checkout и webhooks."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import stripe
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from django_stripe_plisio.billing.enums import (
    BillingPeriod,
    InvoiceStatus,
    LedgerEntryType,
    PaymentProvider,
)
from django_stripe_plisio.billing.models import Invoice, InvoiceLine, UserEntitlement
from django_stripe_plisio.billing.money import (
    from_stripe_amount,
    stripe_amount_is_representable,
    to_stripe_amount,
)
from django_stripe_plisio.billing.services import mark_invoice_paid, record_ledger_entry
from django_stripe_plisio.billing.utils import user_external_id
from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.exceptions import BillingError, WebhookVerificationError
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
        # Импорт ленивый: payments.services импортирует этот модуль
        from django_stripe_plisio.payments.services import validate_invoice_for_checkout

        self._require_redirect_urls()

        with transaction.atomic():
            locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
            validate_invoice_for_checkout(locked)
            previous = self._latest_attempt(locked, statuses=[PaymentAttemptStatus.PENDING])
            previous_pk: int | None = None
            previous_external_id = ""
            if previous is not None and previous.external_id:
                previous_pk = previous.pk
                previous_external_id = previous.external_id

        # HTTP вне блокировки строки счёта
        if previous_external_id and previous_pk is not None:
            reused = self._resolve_previous_session(previous_pk, previous_external_id)
            if reused is not None:
                return reused

        invoice.refresh_from_db()
        try:
            session_params = self._session_params(invoice)
            session = stripe.checkout.Session.create(api_key=self._api_key, **session_params)
        except (BillingError, ImproperlyConfigured):
            raise
        except stripe.StripeError as exc:
            return self._persist_failed_attempt(
                invoice,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )

        with transaction.atomic():
            locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
            try:
                validate_invoice_for_checkout(locked)
            except BillingError:
                return PaymentAttempt.objects.create(
                    invoice=locked,
                    provider=self.provider,
                    status=PaymentAttemptStatus.FAILED,
                    error_code="InvoiceNotPayable",
                    error_message=(
                        f"Invoice {locked.pk} is not payable after Stripe session {session.id}"
                    ),
                    response_payload={"id": session.id, "url": session.url},
                )
            winner = self._latest_attempt(locked, statuses=[PaymentAttemptStatus.PENDING])
            if winner is not None and winner.external_id and winner.pk != previous_pk:
                logger.warning(
                    "Stripe session %s left unused; invoice %s already has attempt %s",
                    session.id,
                    locked.pk,
                    winner.pk,
                )
                return winner
            attempt = PaymentAttempt.objects.create(
                invoice=locked,
                provider=self.provider,
                status=PaymentAttemptStatus.PENDING,
                external_id=session.id,
                payment_url=session.url or "",
                request_payload=session_params,
                response_payload={"id": session.id, "url": session.url},
            )
            locked.payment_url = attempt.payment_url
            locked.external_id = session.id
            locked.save(update_fields=["payment_url", "external_id", "updated_at"])
            return attempt

    def _resolve_previous_session(self, attempt_pk: int, external_id: str) -> PaymentAttempt | None:
        """Открытую сессию возвращаем; завершённую проводим; истёкшую отпускаем под новую."""
        try:
            session = stripe.checkout.Session.retrieve(external_id, api_key=self._api_key)
        except stripe.StripeError:
            logger.warning("Stripe session %s retrieve failed, reuse it", external_id)
            return PaymentAttempt.objects.get(pk=attempt_pk)

        obj = _as_dict(session)
        status = obj.get("status")
        if status == "open":
            return PaymentAttempt.objects.get(pk=attempt_pk)
        if status == "complete":
            self._apply_checkout_session_paid(obj)
            return PaymentAttempt.objects.get(pk=attempt_pk)

        with transaction.atomic():
            PaymentAttempt.objects.filter(
                pk=attempt_pk,
                status=PaymentAttemptStatus.PENDING,
            ).update(status=PaymentAttemptStatus.CANCELLED, updated_at=timezone.now())
        return None

    def _persist_failed_attempt(
        self,
        invoice: Invoice,
        *,
        error_code: str,
        error_message: str,
    ) -> PaymentAttempt:
        with transaction.atomic():
            locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
            return PaymentAttempt.objects.create(
                invoice=locked,
                provider=self.provider,
                status=PaymentAttemptStatus.FAILED,
                error_code=error_code,
                error_message=error_message,
            )

    def _require_redirect_urls(self) -> tuple[str, str]:
        success_url = PackageSettings.success_url()
        cancel_url = PackageSettings.cancel_url()
        if not success_url or not cancel_url:
            raise ImproperlyConfigured(
                "DJANGO_STRIPE_PLISIO_SUCCESS_URL and "
                "DJANGO_STRIPE_PLISIO_CANCEL_URL are required",
            )
        return success_url, cancel_url

    def _assert_chargeable_amount(self, invoice: Invoice, line: InvoiceLine | None) -> None:
        amounts = [invoice.total_minor]
        if line is not None:
            amounts.append(line.unit_amount_minor)
        if invoice.discount_minor:
            amounts.append(invoice.discount_minor)
        for amount in amounts:
            if not stripe_amount_is_representable(amount, invoice.currency):
                raise BillingError(
                    f"Amount {amount} {invoice.currency} is not representable as a Stripe amount",
                )

    def _uses_remote_stripe_price(
        self,
        invoice: Invoice,
        line: InvoiceLine | None,
        mode: str,
    ) -> bool:
        if line is None or line.price is None or not line.price.stripe_price_id:
            return False
        if mode == "subscription":
            return True
        return invoice.discount_minor == 0

    def _assert_stripe_price(self, invoice: Invoice, line: InvoiceLine | None, mode: str) -> None:
        """Локальная цена и Stripe Price должны совпасть до создания сессии."""
        if not self._uses_remote_stripe_price(invoice, line, mode):
            return
        assert line is not None and line.price is not None
        remote = stripe.Price.retrieve(line.price.stripe_price_id, api_key=self._api_key)
        obj = _as_dict(remote)
        currency = str(obj.get("currency") or "").upper()
        unit_amount = obj.get("unit_amount")
        expected = to_stripe_amount(line.unit_amount_minor, invoice.currency)
        if currency != invoice.currency or unit_amount is None or int(unit_amount) != expected:
            raise BillingError(
                f"Stripe price {line.price.stripe_price_id} amount/currency "
                f"does not match invoice {invoice.pk}",
            )

    def _session_params(self, invoice: Invoice) -> dict[str, Any]:
        line = self._first_line(invoice)
        mode = self._checkout_mode(line)
        success_url, cancel_url = self._require_redirect_urls()
        self._assert_chargeable_amount(invoice, line)
        self._assert_stripe_price(invoice, line, mode)
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
                        "unit_amount": to_stripe_amount(line.unit_amount_minor, invoice.currency),
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
                    "unit_amount": to_stripe_amount(invoice.total_minor, invoice.currency),
                    "product_data": {"name": name},
                },
                "quantity": 1,
            }
        ]

    def _create_once_coupon(self, invoice: Invoice) -> str:
        coupon = stripe.Coupon.create(
            api_key=self._api_key,
            amount_off=to_stripe_amount(invoice.discount_minor, invoice.currency),
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
        elif event_type == "charge.refunded":
            self._handle_charge_refunded(obj)
        elif event_type == "refund.updated":
            self._handle_refund_updated(obj)
        else:
            return WebhookProcessingStatus.SKIPPED
        return WebhookProcessingStatus.PROCESSED

    def _webhook_subject(
        self,
        payload: dict[str, Any],
    ) -> tuple[Invoice | None, PaymentAttempt | None]:
        event_type = str(payload.get("type") or "")
        obj = (payload.get("data") or {}).get("object") or {}
        if not isinstance(obj, dict):
            return None, None
        if event_type.startswith("checkout.session"):
            invoice = self._invoice_from_session(obj)
            attempt = None
            session_id = obj.get("id")
            if invoice is not None and session_id:
                attempt = PaymentAttempt.objects.filter(
                    invoice=invoice,
                    provider=self.provider,
                    external_id=session_id,
                ).first()
            return invoice, attempt
        if event_type.startswith("customer.subscription"):
            return self._invoice_from_session({"metadata": obj.get("metadata") or {}}), None
        if event_type in {"charge.refunded", "refund.updated"}:
            return self._invoice_from_charge_like(obj), None
        return None, None

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
        expected = to_stripe_amount(invoice.total_minor, invoice.currency)
        parsed_amount = None if amount_total is None else int(amount_total)
        if not currency or currency != invoice.currency:
            reason = "currency_mismatch"
        elif parsed_amount is None or parsed_amount != expected:
            reason = "amount_mismatch"
        else:
            reason = ""
        if reason:
            logger.error(
                "Stripe session %s amount %s %s does not match invoice %s (%s %s, stripe %s)",
                session_id,
                amount_total,
                currency,
                invoice.pk,
                invoice.total_minor,
                invoice.currency,
                expected,
            )
            send_on_commit(
                payment_mismatch,
                sender=Invoice,
                invoice=invoice,
                provider=self.provider,
                payload=obj,
                reason=reason,
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
                "amount_minor": from_stripe_amount(parsed_amount or 0, currency),
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

    def _invoice_from_charge_like(self, obj: dict[str, Any]) -> Invoice | None:
        metadata = obj.get("metadata") or {}
        invoice_id = metadata.get("invoice_id")
        if not invoice_id:
            return None
        return self._find_invoice(invoice_id)

    def _revoke_invoice_access(self, invoice: Invoice) -> None:
        now = timezone.now()
        for entitlement in UserEntitlement.objects.filter(invoice=invoice):
            if entitlement.active_until is None or entitlement.active_until > now:
                entitlement.active_until = now
                entitlement.save(update_fields=["active_until"])

    def _record_stripe_refund(
        self,
        invoice: Invoice,
        refund_id: str,
        amount: int,
        currency: str,
    ) -> None:
        if currency != invoice.currency:
            logger.error(
                "Stripe refund %s currency %s does not match invoice %s (%s)",
                refund_id,
                currency,
                invoice.pk,
                invoice.currency,
            )
            return
        package_amount = from_stripe_amount(amount, currency)
        if package_amount <= 0:
            return
        record_ledger_entry(
            user=invoice.user,
            entry_type=LedgerEntryType.REFUND,
            amount_minor=-package_amount,
            currency=invoice.currency,
            invoice=invoice,
            reference=f"stripe-refund:{refund_id}",
            note="Stripe refund",
        )

    def _handle_charge_refunded(self, charge: dict[str, Any]) -> None:
        invoice = self._invoice_from_charge_like(charge)
        if invoice is None:
            logger.info("Stripe charge %s refund is not linked to an invoice", charge.get("id"))
            return
        currency = str(charge.get("currency") or "").upper()
        refunds = (charge.get("refunds") or {}).get("data") or []
        for refund in refunds:
            status = refund.get("status") or "succeeded"
            if status not in {"succeeded", "pending"}:
                continue
            refund_id = str(refund.get("id") or "")
            amount = refund.get("amount")
            if not refund_id or amount is None:
                continue
            self._record_stripe_refund(invoice, refund_id, int(amount), currency)
        charged = int(charge.get("amount") or 0)
        refunded = int(charge.get("amount_refunded") or 0)
        if charged and refunded >= charged:
            self._revoke_invoice_access(invoice)

    def _handle_refund_updated(self, refund: dict[str, Any]) -> None:
        if refund.get("status") != "succeeded":
            return
        invoice = self._invoice_from_charge_like(refund)
        if invoice is None:
            logger.info("Stripe refund %s is not linked to an invoice", refund.get("id"))
            return
        refund_id = str(refund.get("id") or "")
        amount = refund.get("amount")
        currency = str(refund.get("currency") or "").upper()
        if not refund_id or amount is None:
            return
        self._record_stripe_refund(invoice, refund_id, int(amount), currency)

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

    def load_remote_invoice_state(self, invoice: Invoice) -> Any:
        if invoice.status != InvoiceStatus.PENDING or not invoice.external_id:
            return InvoiceSyncOutcome.UNCHANGED
        try:
            session = stripe.checkout.Session.retrieve(invoice.external_id, api_key=self._api_key)
        except stripe.StripeError as exc:
            logger.error("Stripe session retrieve failed for invoice %s: %s", invoice.pk, exc)
            return InvoiceSyncOutcome.ERROR
        return _as_dict(session)

    def apply_remote_invoice_state(self, invoice: Invoice, remote: Any) -> InvoiceSyncOutcome:
        if isinstance(remote, InvoiceSyncOutcome):
            return remote
        if invoice.status != InvoiceStatus.PENDING:
            return InvoiceSyncOutcome.UNCHANGED
        obj = remote
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

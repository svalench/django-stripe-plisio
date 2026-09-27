from django.db import transaction
from django.utils import timezone

from django_stripe_plisio.billing.enums import BillingPeriod, InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Invoice, InvoiceLine
from django_stripe_plisio.billing.services import mark_invoice_paid
from django_stripe_plisio.exceptions import BillingError, PaymentProviderError
from django_stripe_plisio.payments.enums import PaymentAttemptStatus
from django_stripe_plisio.payments.models import PaymentAttempt
from django_stripe_plisio.payments.services.base import BasePaymentProvider
from django_stripe_plisio.payments.services.plisio_service import PlisioPaymentService
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService
from django_stripe_plisio.signals import payment_failed, send_on_commit


def get_payment_service(provider: str) -> BasePaymentProvider:
    if provider == PaymentProvider.STRIPE:
        return StripePaymentService()
    if provider == PaymentProvider.PLISIO:
        return PlisioPaymentService()
    raise BillingError(f"Unknown provider: {provider}")


def validate_invoice_for_checkout(invoice: Invoice) -> None:
    """Проверки перед созданием сессии оплаты."""
    if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.PENDING):
        raise BillingError(f"Invoice {invoice.pk} is not payable (status={invoice.status})")
    if invoice.expires_at and invoice.expires_at < timezone.now():
        raise BillingError(f"Invoice {invoice.pk} is not payable (expired)")


def _requires_provider_checkout(invoice: Invoice) -> bool:
    """Нулевой разовый счёт закрываем сразу; подписке нужен Stripe даже при 100% скидке."""
    if invoice.total_minor > 0:
        return True
    if invoice.provider != PaymentProvider.STRIPE:
        return False
    line = InvoiceLine.objects.filter(invoice=invoice).select_related("price").first()
    return bool(line and line.price and line.price.billing_period != BillingPeriod.ONE_TIME)


def _complete_free_invoice(invoice: Invoice) -> PaymentAttempt:
    attempt = PaymentAttempt.objects.create(
        invoice=invoice,
        provider=invoice.provider,
        status=PaymentAttemptStatus.SUCCEEDED,
        response_payload={"free": True},
    )
    mark_invoice_paid(invoice)
    return attempt


def create_checkout(invoice: Invoice) -> PaymentAttempt:
    """Публичная точка входа для создания checkout у провайдера счёта.

    Повторный вызов для того же счёта возвращает уже открытую сессию/инвойс.
    Ошибка провайдера → ``PaymentProviderError`` (попытка FAILED сохранена в БД).
    """
    if invoice.provider not in PaymentProvider.values:
        raise BillingError(f"Invalid invoice provider: {invoice.provider}")
    service = get_payment_service(invoice.provider)

    with transaction.atomic():
        # Блокировка счёта: параллельные запросы не создадут две оплачиваемые сессии
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        validate_invoice_for_checkout(locked)
        if _requires_provider_checkout(locked):
            attempt = service.create_checkout(locked)
        else:
            attempt = _complete_free_invoice(locked)

    # Сервис менял заблокированную копию; объект вызывающего кода должен видеть payment_url
    invoice.refresh_from_db()
    if attempt.status == PaymentAttemptStatus.FAILED:
        send_on_commit(payment_failed, sender=PaymentAttempt, attempt=attempt, invoice=locked)
        raise PaymentProviderError(attempt.error_message or "Payment provider error", attempt)
    return attempt

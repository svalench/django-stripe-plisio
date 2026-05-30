from django_stripe_plisio.billing.enums import InvoiceStatus, PaymentProvider
from django_stripe_plisio.billing.models import Invoice
from django_stripe_plisio.payments.models import PaymentAttempt
from django_stripe_plisio.payments.services.plisio_service import PlisioPaymentService
from django_stripe_plisio.payments.services.stripe_service import StripePaymentService


def get_payment_service(provider: str):
    if provider == PaymentProvider.STRIPE:
        return StripePaymentService()
    if provider == PaymentProvider.PLISIO:
        return PlisioPaymentService()
    raise ValueError(f"Unknown provider: {provider}")


def validate_invoice_for_checkout(invoice: Invoice) -> None:
    """Проверки перед созданием сессии оплаты."""
    if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.PENDING):
        raise ValueError(f"Invoice {invoice.pk} is not payable (status={invoice.status})")


def create_checkout(invoice: Invoice) -> PaymentAttempt:
    """Публичная точка входа для создания checkout у провайдера счёта."""
    if invoice.provider not in PaymentProvider.values:
        raise ValueError(f"Invalid invoice provider: {invoice.provider}")
    validate_invoice_for_checkout(invoice)
    service = get_payment_service(invoice.provider)
    return service.create_checkout(invoice)

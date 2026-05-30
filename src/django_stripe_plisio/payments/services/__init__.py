from django_stripe_plisio.billing.enums import PaymentProvider
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


def create_checkout(invoice: Invoice) -> PaymentAttempt:
    """Публичная точка входа для создания checkout у провайдера счёта."""
    service = get_payment_service(invoice.provider)
    return service.create_checkout(invoice)

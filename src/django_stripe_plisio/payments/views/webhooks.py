"""Webhook endpoints для Stripe и Plisio."""

import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.exceptions import WebhookVerificationError
from django_stripe_plisio.payments.services import get_payment_service
from django_stripe_plisio.payments.services.plisio_service import PlisioPaymentService

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(View):
    def post(self, request, *args, **kwargs):
        service = get_payment_service(PaymentProvider.STRIPE)
        try:
            event = service.verify_webhook(request.body, dict(request.headers))
            service.handle_webhook_event(event)
        except WebhookVerificationError:
            logger.warning("Stripe webhook verification failed")
            return HttpResponseBadRequest()
        except Exception:
            logger.exception("Stripe webhook handler error")
            return HttpResponseBadRequest()
        return HttpResponse(status=200)


@method_decorator(csrf_exempt, name="dispatch")
class PlisioWebhookView(View):
    def post(self, request, *args, **kwargs):
        service = get_payment_service(PaymentProvider.PLISIO)
        if not isinstance(service, PlisioPaymentService):
            return HttpResponseBadRequest()

        try:
            if request.POST:
                event = service.verify_webhook_from_post(request.POST.dict())
            else:
                event = service.verify_webhook(request.body, dict(request.headers))
            service.handle_webhook_event(event)
        except WebhookVerificationError:
            logger.warning("Plisio webhook verification failed")
            return HttpResponseBadRequest()
        except Exception:
            logger.exception("Plisio webhook handler error")
            return HttpResponseBadRequest()
        return HttpResponse(status=200)

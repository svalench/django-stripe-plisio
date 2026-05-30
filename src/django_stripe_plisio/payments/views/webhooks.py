"""Webhook endpoints для Stripe и Plisio."""

import json

from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.payments.services import get_payment_service


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(View):
    def post(self, request, *args, **kwargs):
        service = get_payment_service(PaymentProvider.STRIPE)
        payload = request.body
        try:
            event = service.verify_webhook(payload, dict(request.headers))
            service.handle_webhook_event(event)
        except Exception as exc:
            return HttpResponseBadRequest(str(exc))
        return HttpResponse(status=200)


@method_decorator(csrf_exempt, name="dispatch")
class PlisioWebhookView(View):
    def post(self, request, *args, **kwargs):
        service = get_payment_service(PaymentProvider.PLISIO)
        try:
            if request.content_type and "json" in request.content_type:
                raw = request.body
            else:
                raw = json.dumps(request.POST.dict()).encode()
            event = service.verify_webhook(raw, dict(request.headers))
            service.handle_webhook_event(event)
        except Exception as exc:
            return HttpResponseBadRequest(str(exc))
        return HttpResponse(status=200)

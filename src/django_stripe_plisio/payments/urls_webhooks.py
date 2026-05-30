from django.urls import path

from django_stripe_plisio.payments.views.webhooks import PlisioWebhookView, StripeWebhookView

urlpatterns = [
    path("stripe/", StripeWebhookView.as_view(), name="dsp-webhook-stripe"),
    path("plisio/", PlisioWebhookView.as_view(), name="dsp-webhook-plisio"),
]

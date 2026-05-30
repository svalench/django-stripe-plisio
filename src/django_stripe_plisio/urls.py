"""Корневые URL пакета: API (если DRF) и webhooks."""

from django.urls import include, path

urlpatterns = [
    path("webhooks/", include("django_stripe_plisio.payments.urls_webhooks")),
]

try:
    import rest_framework  # noqa: F401
except ImportError:
    pass
else:
    urlpatterns.insert(0, path("", include("django_stripe_plisio.api.urls")))

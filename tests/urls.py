"""URLconf для pytest: маршруты пакета под префиксом /billing/."""

from django.urls import include, path

urlpatterns = [
    path("billing/", include("django_stripe_plisio.urls")),
]

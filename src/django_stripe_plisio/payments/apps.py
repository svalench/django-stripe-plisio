from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_stripe_plisio.payments"
    label = "dsp_payments"
    verbose_name = "Payments"

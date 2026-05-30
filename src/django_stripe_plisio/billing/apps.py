from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_stripe_plisio.billing"
    label = "dsp_billing"
    verbose_name = "Billing"

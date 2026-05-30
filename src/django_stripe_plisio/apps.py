from django.apps import AppConfig


class DjangoStripePlisioConfig(AppConfig):
    """Корневая конфигурация пакета; billing и payments — отдельные apps."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "django_stripe_plisio"
    label = "django_stripe_plisio"
    verbose_name = "Django Stripe Plisio"

    def ready(self) -> None:
        # Регистрация сигналов при старте Django
        from django_stripe_plisio import signals  # noqa: F401

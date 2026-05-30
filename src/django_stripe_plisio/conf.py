"""Чтение настроек пакета из Django settings с безопасными defaults."""

from django.conf import settings


def _get(name: str, default=None):
    return getattr(settings, name, default)


class PackageSettings:
    """Обёртка над DJANGO_STRIPE_PLISIO_* константами."""

    PREFIX = "DJANGO_STRIPE_PLISIO_"

    @classmethod
    def stripe_secret_key(cls) -> str:
        return _get(f"{cls.PREFIX}STRIPE_SECRET_KEY", "") or ""

    @classmethod
    def stripe_webhook_secret(cls) -> str:
        return _get(f"{cls.PREFIX}STRIPE_WEBHOOK_SECRET", "") or ""

    @classmethod
    def plisio_api_key(cls) -> str:
        return _get(f"{cls.PREFIX}PLISIO_API_KEY", "") or ""

    @classmethod
    def plisio_callback_secret(cls) -> str:
        return _get(f"{cls.PREFIX}PLISIO_CALLBACK_SECRET", "") or ""

    @classmethod
    def default_currency(cls) -> str:
        return (_get(f"{cls.PREFIX}DEFAULT_CURRENCY", "USD") or "USD").upper()

    @classmethod
    def allowed_currencies(cls) -> list[str]:
        raw = _get(f"{cls.PREFIX}ALLOWED_CURRENCIES", None)
        if raw is None:
            return [cls.default_currency()]
        return [c.upper() for c in raw]

    @classmethod
    def success_url(cls) -> str:
        return _get(f"{cls.PREFIX}SUCCESS_URL", "") or ""

    @classmethod
    def cancel_url(cls) -> str:
        return _get(f"{cls.PREFIX}CANCEL_URL", "") or ""

    @classmethod
    def user_id_field(cls) -> str:
        return _get(f"{cls.PREFIX}USER_ID_FIELD", "pk") or "pk"

    @classmethod
    def url_prefix(cls) -> str:
        return _get(f"{cls.PREFIX}URL_PREFIX", "billing") or "billing"

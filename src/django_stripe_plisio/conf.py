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
    def plisio_webhook_url(cls) -> str:
        return _get(f"{cls.PREFIX}PLISIO_WEBHOOK_URL", "") or ""

    @classmethod
    def require_webhook_secret(cls) -> bool:
        return bool(_get(f"{cls.PREFIX}REQUIRE_WEBHOOK_SECRET", True))

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
        """Префикс mount point в проекте-потребителе (только документация)."""
        return _get(f"{cls.PREFIX}URL_PREFIX", "billing") or "billing"

    @classmethod
    def invoice_pending_ttl_hours(cls) -> int | None:
        """Через сколько часов pending-счёт истекает (None — не истекает автоматически)."""
        return _get(f"{cls.PREFIX}INVOICE_PENDING_TTL_HOURS", None)

    @classmethod
    def cron_config(cls) -> dict:
        """Периодические задачи: sync_invoices, expire_invoices (enabled + schedule)."""
        raw = _get(f"{cls.PREFIX}CRON", None)
        if raw is None:
            return {}
        return dict(raw)

    @classmethod
    def cron_task_enabled(cls, task_name: str) -> bool:
        task = cls.cron_config().get(task_name) or {}
        return bool(task.get("enabled", False))

    @classmethod
    def cron_task_schedule(cls, task_name: str) -> str:
        task = cls.cron_config().get(task_name) or {}
        return str(task.get("schedule", "") or "")

    @classmethod
    def invoice_sync_batch_size(cls) -> int:
        value = _get(f"{cls.PREFIX}INVOICE_SYNC_BATCH_SIZE", 100)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 100

    @classmethod
    def invoice_sync_providers(cls) -> list[str] | None:
        """None — все провайдеры; иначе список stripe/plisio."""
        raw = _get(f"{cls.PREFIX}INVOICE_SYNC_PROVIDERS", None)
        if raw is None:
            return None
        return [str(p).lower() for p in raw]

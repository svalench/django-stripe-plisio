"""Сборка CRONJOBS для django-crontab из DJANGO_STRIPE_PLISIO_CRON."""

from __future__ import annotations

from django_stripe_plisio.conf import PackageSettings

# Имена задач в DJANGO_STRIPE_PLISIO_CRON → management commands
_CRON_COMMANDS: dict[str, str] = {
    "sync_invoices": "dsp_sync_invoices",
    "expire_invoices": "dsp_expire_invoices",
}


def build_cronjobs(extra: list | None = None) -> list:
    """
    Собрать CRONJOBS для django-crontab.

    Пример settings.py::

        from django_stripe_plisio.cron import build_cronjobs

        CRONJOBS = build_cronjobs()

    Задачи с ``enabled: False`` не попадают в список.
    Для sync_invoices нужны ключи провайдера в settings проекта — иначе команда
    завершится с errors=0, но опрос не выполнится (см. документацию пакета).
    """
    jobs: list = []
    for task_name, command in _CRON_COMMANDS.items():
        if not PackageSettings.cron_task_enabled(task_name):
            continue
        schedule = PackageSettings.cron_task_schedule(task_name)
        if not schedule:
            continue
        jobs.append(
            (schedule, "django.core.management.call_command", [command]),
        )

    if extra:
        jobs.extend(extra)
    return jobs

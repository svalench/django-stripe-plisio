"""Management command: опрос провайдеров и обновление статусов pending-счетов."""

from django.core.management.base import BaseCommand

from django_stripe_plisio.conf import PackageSettings
from django_stripe_plisio.payments.services.invoice_sync import sync_pending_invoices


class Command(BaseCommand):
    help = "Опросить Stripe/Plisio и обновить статусы pending-счетов с external_id"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Не записывать в БД, только логировать",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Лимит счетов за прогон (по умолчанию из настроек)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Запустить даже если sync_invoices выключен в DJANGO_STRIPE_PLISIO_CRON",
        )

    def handle(self, *args, **options):
        if not options["force"] and not PackageSettings.cron_task_enabled("sync_invoices"):
            self.stdout.write(
                self.style.WARNING(
                    "sync_invoices is disabled in DJANGO_STRIPE_PLISIO_CRON; skipping "
                    "(use --force for a manual run)",
                ),
            )
            return

        result = sync_pending_invoices(
            batch_size=options["batch_size"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Sync done: "
                f"checked={result.checked} paid={result.paid} "
                f"expired={result.expired_remote} cancelled={result.cancelled_remote} "
                f"skipped={result.skipped} errors={result.errors}",
            ),
        )

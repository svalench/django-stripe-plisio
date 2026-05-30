"""Management command: истечение просроченных pending-счетов."""

from django.core.management.base import BaseCommand

from django_stripe_plisio.billing.services import expire_pending_invoices


class Command(BaseCommand):
    help = "Перевести pending-счета с истёкшим expires_at в статус expired"

    def handle(self, *args, **options):
        count = expire_pending_invoices()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} invoice(s)"))

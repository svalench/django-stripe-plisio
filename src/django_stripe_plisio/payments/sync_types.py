"""Типы результата опроса статуса счёта у провайдера."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InvoiceSyncOutcome(StrEnum):
    UNCHANGED = "unchanged"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class SyncResult:
    checked: int = 0
    paid: int = 0
    expired_remote: int = 0
    cancelled_remote: int = 0
    skipped: int = 0
    errors: int = 0

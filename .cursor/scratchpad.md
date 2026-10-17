# Scratchpad

## 0.5.0 — исправления ревью

- [x] Plisio: сумма `source_amount`/`source_currency`, `return_existing`, `mismatch`/`expired` partial, `error`, без `confirmed`
- [x] TTL не закрывает счета с `external_id`; sync шлёт `payment_mismatch`
- [x] `active_until` 30/365 дней; Plisio + month/year → `BillingError`
- [x] Stripe: равенство суммы, UGX ×100, сверка `stripe_price_id`, refund в ledger
- [x] фиксированная скидка требует валюту; пустые SUCCESS/CANCEL URL не подменяются
- [x] HTTP вне `select_for_update`; `WebhookEvent.invoice` / `payment_attempt`
- [x] pytest 94/94: Django 5.2.12 (3.12), Django 6.1.2 (3.12), Python 3.11.17 + Django 5.2.18, Python 3.13.16 + Django 6.1.2
- [x] ruff, mypy, makemigrations --check, twine check, wheel с миграциями, license SPDX
- [x] версия 0.5.0, CHANGELOG, README, публикация через GitHub Release `v0.5.0`

DONE

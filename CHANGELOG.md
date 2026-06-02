# Changelog

## 0.3.0

### Added

- Опрос статусов pending-счетов у Stripe/Plisio (`sync_pending_invoices`, `sync_invoice_status`).
- Management command `dsp_sync_invoices` (`--dry-run`, `--batch-size`).
- Настройки `DJANGO_STRIPE_PLISIO_CRON`, `INVOICE_SYNC_BATCH_SIZE`, `INVOICE_SYNC_PROVIDERS`.
- `django_stripe_plisio.cron.build_cronjobs()` для django-crontab.
- Optional extra `[cron]` → `django-crontab>=2.4`.

## 0.2.0

### Security

- Stripe webhook: обязательная проверка подписи (`STRIPE_WEBHOOK_SECRET`), fail-closed.
- Plisio callback: обязательный `verify_hash` при `REQUIRE_WEBHOOK_SECRET=True`.
- Webhook HTTP views не отдают текст исключения клиенту.

### Fixed

- Гонки webhook: `select_for_update` на invoice и webhook event.
- Идемпотентность ledger: уникальный `reference`, повтор не дублирует проводку.
- Plisio `callback_url` → `PLISIO_WEBHOOK_URL` (отдельно от `SUCCESS_URL`).
- Сумма Plisio: корректная конвертация для JPY и zero-decimal валют.
- Промокод `used_count` увеличивается только после оплаты.
- Stripe subscription checkout: `recurring` в `price_data` при отсутствии `stripe_price_id`.
- Обработка webhook `customer.subscription.*` → `StripeSubscription`.

### Added

- `DJANGO_STRIPE_PLISIO_PLISIO_WEBHOOK_URL`
- `DJANGO_STRIPE_PLISIO_REQUIRE_WEBHOOK_SECRET`
- `DJANGO_STRIPE_PLISIO_INVOICE_PENDING_TTL_HOURS`
- `USER_ID_FIELD` в metadata Stripe
- Management command `dsp_expire_invoices`
- `billing/money.py`, `expire_pending_invoices`
- LICENSE, расширенные тесты

### Removed

- Optional extra `[celery]` без реализации
- Устаревший `default_app_config` в `__init__.py`

## 0.1.0

- Первый релиз: billing, Stripe, Plisio, DRF API, admin.

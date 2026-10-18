# Changelog

## 0.5.1

### Fixed

- Plisio callbacks with non-ASCII or non-string signatures now return HTTP 400 instead of raising an unhandled verification error.
- Non-finite, negative and out-of-range provider amounts are rejected safely and recorded for review without crediting invoices or overflowing database amounts.
- The `cron` extra now requires the available `django-crontab>=0.7.1`, fixing an unsatisfiable dependency on a nonexistent 2.4 release.

### Security

- Pass the release tag through an environment variable before shell version validation, avoiding evaluation of tag content as shell code.

## 0.5.0

### Fixed

- Plisio `completed` зачисляется только если `source_amount` и `source_currency` совпадают со счётом. Меньшая, большая или пустая сумма → `payment_mismatch`, счёт остаётся `pending`.
- `return_existing` больше не привязывает старый инвойс Plisio с другой суммой: попытка сохраняется как `failed`.
- `mismatch` и `expired` с положительным `amount` не закрывают счёт молча: `payment_mismatch`, `ProviderTransaction` с сырым callback. В `expired` переводится только нулевая сумма.
- Plisio `error` переводит счёт и попытку в `failed` и шлёт `payment_failed`. `pending internal` счёт не трогает.
- Локальный TTL (`expire_pending_invoices`) не истекает счета с непустым `external_id`. Их закрывает sync после ответа провайдера.
- Фиксированная скидка без `fixed_currency` или с другой валютой не применяется (`BillingError` при создании счёта).
- Stripe: оплата только при точном равенстве `amount_total` и валюты. Переплата — `payment_mismatch`, не `paid`.
- Stripe: `UGX` (и `ISK` в нулевых единицах пакета) передаётся в единицах Stripe (`×100`). Трёхзначные валюты с ненулевым последним знаком отклоняются.
- `Price.stripe_price_id` сверяется со Stripe Price (`unit_amount`, `currency`) до создания сессии.
- `charge.refunded` / `refund.updated`: идемпотентное списание в ledger (`reference=stripe-refund:{id}`). Полный возврат снимает доступ (`active_until=now`).
- HTTP к Stripe и Plisio не держит `select_for_update`. Запись попытки и статуса — короткая транзакция после ответа; ошибка БД одного счёта в sync не ломает остальные.

### Changed

- Plisio и цена `month`/`year`: `create_invoice` поднимает `BillingError`. Рекуррент только через Stripe.
- `mark_invoice_paid` для `month`/`year` ставит `UserEntitlement.active_until` на 30 или 365 дней. Webhook подписки Stripe по-прежнему заменяет его на `current_period_end`.
- Статус Plisio `confirmed` больше не считается оплатой. Оплата — только `completed` с совпавшей суммой.
- Пустые `SUCCESS_URL` / `CANCEL_URL` не подменяются на `example.com`. Для Stripe Checkout обе настройки обязательны (`ImproperlyConfigured`).
- `WebhookEvent.invoice` и `WebhookEvent.payment_attempt` заполняются, когда обработчик их определил.
- `project.license` — SPDX-строка `"MIT"`.

## 0.4.0

### Security

- Stripe: подпись webhook проверяется `WebhookSignature.verify_header` с `DEFAULT_TOLERANCE` (защита от replay); `Webhook.construct_event` в stripe-python 15 падал с `500` на подписанных событиях.
- Plisio: проверка `verify_hash` по алгоритму Plisio — HMAC-SHA1 от JSON (`json=true` добавляется в `callback_url`) или от PHP `serialize()` для form-callback; сравнение `hmac.compare_digest`. Прежний `sha1(urlencode + secret)` отклонял настоящие callbacks.
- Plisio: при заданном секрете `verify_hash` обязателен даже при `REQUIRE_WEBHOOK_SECRET=False`.
- Plisio: `api_key` больше не попадает в `PaymentAttempt.error_message` и логи.

### Fixed

- Stripe: при `stripe_price_id` учитываются количество и скидка; скидка подписки — разовым купоном.
- Stripe: оплата засчитывается только при `payment_status=paid` и совпадении `amount_total`/`currency` со счётом; обработка `checkout.session.async_payment_*` и `checkout.session.expired`.
- Stripe: `subscription_data.metadata` — события `customer.subscription.*` обновляют статус и период; `UserEntitlement.active_until` продлевается и отзывается по статусу подписки.
- `payment_url` — `max_length=2048` (URL Stripe Checkout длиннее 200 символов, на PostgreSQL был `DataError`).
- Превышение лимита промокода больше не откатывает зачисление оплаты; неоплаченные счета резервируют использование промокода.
- `record_ledger_entry`: savepoint вокруг INSERT — `IntegrityError` не ломает внешнюю транзакцию PostgreSQL; чужой `reference` → ошибка.
- Статус `failed` у `WebhookEvent` сохраняется при ошибке обработчика; webhook views отвечают `500` на ошибки обработки.
- `sync_pending_invoices`: транзакция на каждый счёт вместо одной на всю пачку.
- Повторный `create_checkout` возвращает открытую сессию/инвойс вместо создания второй оплачиваемой; счёт блокируется на время создания.
- Счёт с нулевой суммой (разовая цена) закрывается без провайдера.
- `apply_promo` атомарен и запрещён после создания checkout.
- Промокод: точное совпадение кода приоритетнее регистронезависимого (без `MultipleObjectsReturned`).
- Plisio: `SUCCESS_URL`/`CANCEL_URL` → `success_invoice_url`/`fail_invoice_url` (кнопки возврата), а не server callback.
- `minor_to_major_amount` на `Decimal`; валюты с 3 знаками (KWD, BHD, …).
- Публичный API `from django_stripe_plisio import api` снова импортируется (модуль `api.py` был перекрыт пакетом `api/`).
- Админка: поиск по `USERNAME_FIELD` кастомной модели пользователя.

### Changed

- Сигналы отправляются после коммита (`on_commit` + `send_robust`).
- Бизнес-ошибки — `BillingError` (подкласс `ValueError`); ошибки провайдера — `PaymentProviderError` с сохранённой попыткой.
- REST API: `400` на бизнес-ошибки, `502` на ошибки провайдера; checkout views объединены.
- `Invoice.status` в админке только для чтения, действие «Отметить оплаченными».
- `Django>=5.2`.

### Added

- Сигналы `payment_mismatch`, `subscription_changed`.
- `StripeSubscription.entitlement`, статусы подписки `unpaid`, `incomplete_expired`, `paused`.
- `dsp_sync_invoices --force`.
- CI: PostgreSQL, матрица Django 5.2 / 6.0, проверка миграций; mypy с плагином django-stubs.

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

# Scratchpad

## GitHub Actions CI + PyPI publish

- [x] tests/settings.py + tests/urls.py + tests/__init__.py
- [x] pyproject.toml: pythonpath [".", "src"]
- [x] .github/workflows/ci.yml + publish.yml
- [x] mypy: stripe session dict/to_dict fix
- [x] ruff: unused PaymentProvider imports removed
- [x] full pipeline local: pytest 30/30, ruff, mypy, build OK (py3.13)

## Ревью проекта (2026-09-27)

- [x] прочитаны все исходники src/, CI, README (частично)
- [x] pytest 30/30, ruff OK, makemigrations --check OK
- [x] mypy: 3 ошибки в billing/services.py (115, 292, 295)
- [x] подтверждено: api.py перекрыт пакетом api/ — публичный API из README не импортируется
- [x] подтверждено по docs Plisio: verify_hash = HMAC-SHA1(JSON|PHP serialize), а не sha1(urlencode+secret)
- [x] исправления: все группы (critical, api, subs, tx, infra)

## Исправления по ревью

- [x] Plisio: HMAC-SHA1 (JSON / PHP serialize), json=true, compare_digest, редакция api_key, return_existing, success_invoice_url
- [x] Stripe: quantity/скидка, купон для подписки, проверка amount/payment_status, async/expired события, reuse сессии, api_key per-call
- [x] Подписки: subscription_data.metadata, StripeSubscription.entitlement, active_until по периоду/отмене
- [x] payment_url max_length=2048 + миграции (billing 0003, payments 0002)
- [x] промокод: резерв pending-счетами, без raise при оплате; ledger savepoint + проверка владельца
- [x] webhook FAILED сохраняется, 500 на ошибки; sync по одному счёту; create_checkout под блокировкой
- [x] сигналы on_commit + send_robust; payment_mismatch, subscription_changed
- [x] публичный API в api/__init__.py; REST 400/502; BillingError / PaymentProviderError
- [x] админка (USERNAME_FIELD, status readonly, action), --force, CI Postgres + Django 5.2/6.0, mypy-плагин, .idea в .gitignore
- [x] pytest 75/75 на SQLite и PostgreSQL (pgserver), ruff, mypy, makemigrations --check
- [x] README + CHANGELOG (Unreleased)

## Проверка в demo_project и релиз 0.4.0

- [x] smoke по HTTP: Stripe/Plisio webhooks с реальной подписью, подписки, REST, админка, management-команды — 21/21
- [x] найдено: `construct_event` (stripe 15) → 500 на подписанном webhook; исправлено на `verify_header` + tolerance, правило `.cursor/rules/webhook-verification.mdc`
- [x] pytest 77/77 на SQLite и PostgreSQL, ruff, mypy, makemigrations --check, build + twine check
- [x] версия 0.4.0 (pyproject, `__version__`, CHANGELOG)
- [ ] push, CI, GitHub release 0.4.0 → PyPI

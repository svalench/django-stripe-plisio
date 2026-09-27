"""Минимальные Django settings для pytest и CI (без demo_project)."""

import os

SECRET_KEY = "test-secret-key-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "django_stripe_plisio",
    "django_stripe_plisio.billing",
    "django_stripe_plisio.payments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

ROOT_URLCONF = "tests.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

# SQLite не проверяет длину varchar и мягче к ошибкам в транзакциях — CI гоняет и PostgreSQL
if os.environ.get("DSP_TEST_DB") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "dsp_test"),
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "postgres"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        },
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Быстрый хэшер: PBKDF2 в create_user замедляет каждый тест на секунды
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

USE_TZ = True
TIME_ZONE = "UTC"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
}

# Базовые значения; conftest переопределяет webhook-секреты через fixture
DJANGO_STRIPE_PLISIO_STRIPE_SECRET_KEY = "sk_test_dummy"
DJANGO_STRIPE_PLISIO_STRIPE_WEBHOOK_SECRET = "whsec_test_secret"
DJANGO_STRIPE_PLISIO_PLISIO_API_KEY = "plisio_test_key"
DJANGO_STRIPE_PLISIO_PLISIO_CALLBACK_SECRET = "plisio_test_secret"
DJANGO_STRIPE_PLISIO_PLISIO_WEBHOOK_URL = "http://testserver/billing/webhooks/plisio/"
DJANGO_STRIPE_PLISIO_REQUIRE_WEBHOOK_SECRET = True

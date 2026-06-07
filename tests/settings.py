"""Минимальные Django settings для pytest и CI (без demo_project)."""

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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

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

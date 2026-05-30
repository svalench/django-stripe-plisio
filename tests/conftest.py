import pytest
from django.contrib.auth import get_user_model


@pytest.fixture(autouse=True)
def dsp_package_settings(settings):
    settings.DJANGO_STRIPE_PLISIO_PLISIO_WEBHOOK_URL = "http://testserver/billing/webhooks/plisio/"
    settings.DJANGO_STRIPE_PLISIO_STRIPE_WEBHOOK_SECRET = "whsec_test_secret"
    settings.DJANGO_STRIPE_PLISIO_PLISIO_CALLBACK_SECRET = "plisio_test_secret"
    settings.DJANGO_STRIPE_PLISIO_REQUIRE_WEBHOOK_SECRET = True


@pytest.fixture
def user(db):
    User = get_user_model()
    return User.objects.create_user(username="testuser", password="testpass")

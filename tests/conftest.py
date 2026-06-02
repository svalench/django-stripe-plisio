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


@pytest.fixture
def product(db):
    from django_stripe_plisio.billing.models import Product

    return Product.objects.create(code="pro", name="Pro Plan")


@pytest.fixture
def price(product):
    from django_stripe_plisio.billing.models import Price

    return Price.objects.create(
        product=product,
        currency="USD",
        amount_minor=1000,
        billing_period="one_time",
    )

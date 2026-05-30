import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from django_stripe_plisio.billing.models import Price, Product


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def catalog(db):
    product = Product.objects.create(code="basic", name="Basic")
    price = Price.objects.create(product=product, currency="USD", amount_minor=999)
    return product, price


@pytest.mark.django_db
def test_products_list(api_client, catalog):
    response = api_client.get("/billing/products/")
    assert response.status_code == 200
    assert len(response.data) >= 1


@pytest.mark.django_db
def test_create_invoice(api_client, catalog, user):
    _, price = catalog
    response = api_client.post(
        "/billing/invoices/",
        {"price_id": price.pk, "quantity": 1, "provider": "stripe"},
        format="json",
    )
    assert response.status_code == 201
    assert response.data["total_minor"] == 999
    assert response.data["provider"] == "stripe"


@pytest.mark.django_db
def test_invoice_list_only_own(api_client, catalog, user):
    _, price = catalog
    api_client.post(
        "/billing/invoices/",
        {"price_id": price.pk, "quantity": 1, "provider": "plisio"},
        format="json",
    )
    other = get_user_model().objects.create_user(username="other", password="x")
    other_client = APIClient()
    other_client.force_authenticate(user=other)
    response = other_client.get("/billing/invoices/")
    assert response.status_code == 200
    assert len(response.data) == 0

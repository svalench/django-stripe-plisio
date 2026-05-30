from django_stripe_plisio.billing.money import minor_to_major_amount


def test_minor_to_major_usd():
    assert minor_to_major_amount(1999, "USD") == "19.99"


def test_minor_to_major_jpy():
    assert minor_to_major_amount(500, "JPY") == "500"

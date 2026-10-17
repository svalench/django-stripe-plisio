from django_stripe_plisio.billing.money import (
    from_stripe_amount,
    minor_to_major_amount,
    stripe_amount_is_representable,
    to_stripe_amount,
)


def test_minor_to_major_usd():
    assert minor_to_major_amount(1999, "USD") == "19.99"


def test_minor_to_major_jpy():
    assert minor_to_major_amount(500, "JPY") == "500"


def test_ugx_stripe_amount_is_scaled():
    assert to_stripe_amount(5, "UGX") == 500
    assert from_stripe_amount(500, "UGX") == 5


def test_usd_stripe_amount_is_unchanged():
    assert to_stripe_amount(1000, "USD") == 1000
    assert from_stripe_amount(1000, "USD") == 1000


def test_three_decimal_stripe_amount_must_end_with_zero():
    assert stripe_amount_is_representable(1000, "KWD") is True
    assert stripe_amount_is_representable(1001, "KWD") is False

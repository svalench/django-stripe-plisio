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


def test_invalid_provider_amounts_are_rejected():
    from django_stripe_plisio.billing.money import major_amount_matches, major_to_minor_amount

    for value in ("NaN", "sNaN", "Infinity", "-Infinity", "1e999999999", "-1"):
        assert major_amount_matches(value, 100, "USD") is False
        assert major_to_minor_amount(value, "USD") is None


def test_provider_amount_database_boundary():
    from django_stripe_plisio.billing.money import major_to_minor_amount

    assert major_to_minor_amount("92233720368547758.07", "USD") == 2**63 - 1
    assert major_to_minor_amount("92233720368547758.08", "USD") is None
    assert major_to_minor_amount("0", "USD") == 0
    assert major_to_minor_amount("0.001", "USD") is None

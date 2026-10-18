"""Конвертация minor units для платёжных провайдеров."""

from decimal import Decimal, InvalidOperation

# ISO 4217: валюты без дробной части
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)

# ISO 4217: валюты с тремя знаками после запятой
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def currency_exponent(currency: str) -> int:
    """Степень 10 для перевода minor → major (0 для JPY, 3 для KWD и т.д.)."""
    code = currency.upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def minor_to_major_amount(amount_minor: int, currency: str) -> str:
    """Строка суммы для API провайдера (Plisio source_amount)."""
    exp = currency_exponent(currency)
    major = Decimal(amount_minor).scaleb(-exp)
    return f"{major:.{exp}f}"


# Stripe хранит ISK/UGX как двухзначные, хотя ISO у них нулевая дробная часть.
# 5 UGX в пакете (exponent 0 → amount_minor=5) уходит в Stripe как 500.
STRIPE_TWO_DECIMAL_COMPAT = frozenset({"ISK", "UGX"})


def to_stripe_amount(amount_minor: int, currency: str) -> int:
    """Minor units пакета → ``unit_amount`` / ``amount_total`` Stripe."""
    code = currency.upper()
    if code in STRIPE_TWO_DECIMAL_COMPAT and currency_exponent(code) == 0:
        return amount_minor * 100
    return amount_minor


def from_stripe_amount(stripe_amount: int, currency: str) -> int:
    """Сумма Stripe → minor units пакета."""
    code = currency.upper()
    if code in STRIPE_TWO_DECIMAL_COMPAT and currency_exponent(code) == 0:
        return stripe_amount // 100
    return stripe_amount


def stripe_amount_is_representable(amount_minor: int, currency: str) -> bool:
    """Трёхзначные валюты Stripe принимает только с последним нулём (фактически 2 знака)."""
    if currency.upper() in THREE_DECIMAL_CURRENCIES:
        return amount_minor % 10 == 0
    return True


def major_amount_matches(raw_amount: object, amount_minor: int, currency: str) -> bool:
    """Совпадает ли десятичная сумма провайдера с minor units счёта."""
    if raw_amount is None or raw_amount == "":
        return False
    try:
        paid = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError):
        return False
    return paid.is_finite() and paid == Decimal(minor_to_major_amount(amount_minor, currency))


def major_to_minor_amount(raw_amount: object, currency: str) -> int | None:
    """Десятичная сумма → minor units. None, если дробная часть не укладывается в exponent."""
    if raw_amount is None or raw_amount == "":
        return None
    try:
        major = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError):
        return None
    # ProviderTransaction.amount_minor is a database PositiveBigIntegerField.
    # Bound before arithmetic/int conversion to reject non-finite and huge exponents.
    exponent = currency_exponent(currency)
    if not major.is_finite() or not 0 <= major <= Decimal(2**63 - 1).scaleb(-exponent):
        return None
    scaled = major * (Decimal(10) ** exponent)
    integral = scaled.to_integral_value()
    if scaled != integral:
        return None
    return int(integral)

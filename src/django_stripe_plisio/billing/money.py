"""Конвертация minor units для платёжных провайдеров."""

from decimal import Decimal

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

"""Конвертация minor units для платёжных провайдеров."""

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


def currency_exponent(currency: str) -> int:
    """Степень 10 для перевода minor → major (0 для JPY и т.д.)."""
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        return 0
    return 2


def minor_to_major_amount(amount_minor: int, currency: str) -> str:
    """Строка суммы для API провайдера (Plisio source_amount)."""
    exp = currency_exponent(currency)
    if exp == 0:
        return str(amount_minor)
    divisor = 10**exp
    major = amount_minor / divisor
    return f"{major:.{exp}f}"

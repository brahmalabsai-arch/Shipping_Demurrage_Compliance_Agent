"""The ISO 4217 check the document verification agent flags ``invalid_currency`` with."""

import pytest

from models.currencies import ISO_4217_CURRENCIES, is_iso_currency


@pytest.mark.parametrize("code", ["USD", "EUR", "JPY", "INR", "KRW", "CNY", "XCG", "ZWG"])
def test_circulating_currencies_are_valid(code: str) -> None:
    assert is_iso_currency(code)


@pytest.mark.parametrize(
    "code",
    [
        "XYZ",  # shaped like a code, assigned to nothing
        "usd",  # codes are upper case
        "BGN",  # withdrawn: Bulgaria adopted the euro on 2026-01-01
        "ANG",  # replaced by XCG
        "XAU",  # gold, not a currency an invoice is settled in
        "XXX",  # "no currency"
        "XTS",  # reserved for testing
    ],
)
def test_non_currencies_are_invalid(code: str) -> None:
    assert not is_iso_currency(code)


def test_every_code_is_three_capital_letters() -> None:
    assert all(len(c) == 3 and c.isascii() and c.isupper() for c in ISO_4217_CURRENCIES)

"""ISO 4217 currency codes a trade document can be settled in.

``CurrencyCode`` (``models.schemas``) only checks the *shape* of a code — three capital letters —
so a value such as ``"XYZ"`` is stored as entered. Whether it names a real currency is a
verification finding, not an input error: the document verification agent flags it as
``invalid_currency`` and never passes it on to FX resolution (edge cases, Phase 7), where it
would otherwise surface as an unrelated-looking "no rate" error.

The set is ISO 4217's active codes for circulating currencies (status 2026), without fund
codes (e.g. ``BOV``, ``CLF``), precious metals (``XAU``...), testing/"no currency" codes
(``XTS``, ``XXX``) or accounting units (``XDR``, ``XSU``, ``XUA``) — none of those can appear
on a commercial invoice. Withdrawn codes (``BGN`` since the euro changeover on 2026-01-01,
``ANG`` replaced by ``XCG`` in 2025, ``HRK``, ``SLL``, ``ZWL``, ``CUC``) are not valid either.
"""

# fmt: off
ISO_4217_CURRENCIES: frozenset[str] = frozenset((
    "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT", "BHD",
    "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF",
    "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN",
    "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD",
    "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES",
    "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL",
    "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN",
    "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP",
    "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK",
    "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT",
    "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VED", "VES",
    "VND", "VUV", "WST", "XAF", "XCD", "XCG", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWG",
))
# fmt: on


def is_iso_currency(code: str) -> bool:
    """True if ``code`` is an active ISO 4217 code for a circulating currency."""
    return code in ISO_4217_CURRENCIES

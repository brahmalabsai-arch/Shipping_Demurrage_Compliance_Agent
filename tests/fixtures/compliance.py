"""The baseline simulated customs bulletin; its sections are also the baseline rulebook.

Six rules relevant to the JNPT auto-parts case study (HS heading 8708 imports into INNSA from
China, Korea, Japan and Germany). All content is synthetic and illustrative — not an official
CBIC publication, and not legal or tariff advice.

The seed publishes this text as the bulletin and imports each section as version 1 of its rule,
approved by the seed (the approval is still a logged ``decide`` call). Tests build edited
bulletins from it with :func:`edit_section`.
"""

import re

BASELINE_BULLETIN = """\
# Customs Bulletin - Nhava Sheva (INNSA) import desk

SIMULATED bulletin for the Port & Customs Clearance Copilot demo. Rates and rules are
illustrative only.

## HSN-8708-duty
Effective: 2026-04-01
HS codes: 8708
Parts and accessories of motor vehicles (heading 8708) attract basic customs duty (BCD) of
15% of the assessable value, a Social Welfare Surcharge of 10% of the BCD, and IGST of 28%
on the assessable value plus BCD and surcharge.

## HSN-870830-brake-certification
Effective: 2026-04-01
HS codes: 870830
Brakes and servo-brakes and their parts (sub-heading 8708 30) are cleared only against a
valid certificate of conformity to the notified automotive safety standard, covering the
manufacturer and model on the invoice. Consignments without it are held for testing.

## DOC-quantity-consistency
Effective: 2026-01-01
HS codes: all
The quantity declared on the commercial invoice, packing list and bill of lading must agree.
The importer must explain any difference before assessment; an unexplained difference puts
the bill of entry on hold for examination.

## DOC-commercial-invoice-required
Effective: 2026-01-01
HS codes: all
A bill of entry cannot be assessed without the commercial invoice. Where the invoice has not
been filed by the time the vessel arrives, the goods remain uncleared and demurrage accrues
to the importer.

## VAL-exchange-rate
Effective: 2026-01-01
HS codes: all
Assessable value in a foreign currency is converted into Indian rupees at the import exchange
rate notified by the customs authority for the period that includes the date the bill of
entry is filed. A revised notification replaces the original rate from its own start date.

## ORG-KR-CEPA
Effective: 2026-01-01
HS codes: 8708
Preferential duty under the India-Korea CEPA on goods of heading 8708 requires a certificate
of origin issued by the Korean authority, quoted on the bill of entry. Without it, the
standard rate under HSN-8708-duty applies.
"""

BASELINE_RULE_KEYS = (
    "HSN-8708-duty",
    "HSN-870830-brake-certification",
    "DOC-quantity-consistency",
    "DOC-commercial-invoice-required",
    "VAL-exchange-rate",
    "ORG-KR-CEPA",
)


def edit_section(bulletin: str, rule_key: str, old: str, new: str) -> str:
    """``bulletin`` with ``old`` replaced by ``new`` inside the section of ``rule_key`` only."""
    pattern = re.compile(rf"(^## {re.escape(rule_key)}\n.*?)(?=^## |\Z)", re.S | re.M)
    match = pattern.search(bulletin)
    if match is None or old not in match.group(1):
        raise KeyError(f"{old!r} not found in section {rule_key}")
    section = match.group(1).replace(old, new, 1)
    return bulletin[: match.start()] + section + bulletin[match.end() :]

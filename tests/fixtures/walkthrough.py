"""Fifteen walkthrough shipments: one case per way the system can stop a shipment, each
recoverable to ``cleared`` (``docs/WALKTHROUGH.md`` gives the steps on the dashboards).

Seeded on demand by ``python -m scripts.seed_walkthrough`` (not by ``scripts.seed``: the four
case-study shipments and their figures stay as documented). Every case is played from problem
to ``cleared`` by ``tests/test_e2e/test_walkthrough.py``, so the guide's steps are checked.

All dates are fixed; the outcomes in the guide are as of the analysis date 2026-10-05. Every
case ships to INNSA. Exchange rates are locked for each case's invoice and bill of lading
dates (:data:`WALKTHROUGH_LOCKS`), except where a missing lock is the point (WT14, WT15).

| Case | Container   | What stops it                                   |
|------|-------------|-------------------------------------------------|
| WT01 | OOLU8101010 | nothing: the straight path                      |
| WT02 | EGHU8102020 | packing list short (quantity mismatch)          |
| WT03 | TGHU8103030 | invoice value differs from the booking          |
| WT04 | CMAU8104040 | invoice in the wrong currency                   |
| WT05 | HLXU8105050 | currency code typo on the invoice               |
| WT06 | MSCU8106060 | bill of lading not attached                     |
| WT07 | ONEU8107070 | still in transit: not at a fileable stage       |
| WT08 | YMLU8108080 | held by customs: not at a fileable stage        |
| WT09 | SEGU8109090 | reefer box with no port tariff (data gap)       |
| WT10 | ZIMU8110100 | no arrival date on the record (data gap)        |
| WT11 | BMOU8111110 | a new rule for its goods waits for review       |
| WT12 | TCNU8112120 | the reviewer rejects the first filing           |
| WT13 | FCIU8113130 | papers change while the filing waits            |
| WT14 | GESU8114140 | only a quotation: the whole lifecycle to go     |
| WT15 | TRHU8115150 | only "to do" items: fileable as it is           |
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from models.enums import ContainerType, DocType, LockType, ShipmentStatus
from models.schemas import DocumentCreate, LockedRateCreate, ShipmentCreate
from tests.fixtures.shipments import FixtureShipment

S = ShipmentStatus
INVOICE, PACKING, BOL = DocType.INVOICE, DocType.PACKING_LIST, DocType.BILL_OF_LADING
TO_DOCKED = (S.BOOKED, S.IN_TRANSIT, S.DOCKED)
StartStatus = Literal[ShipmentStatus.QUOTED, ShipmentStatus.BOOKED]  # how a shipment enters
AS_OF = date(2026, 10, 5)  # the analysis date the guide's figures are for


def _utc(m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(2026, m, d, hh, mm, tzinfo=UTC)


def _ship(
    container_id: str,
    container_type: ContainerType,
    cargo: str,
    hs_code: str,
    *,
    qty: int,
    value: str,
    currency: str,
    origin: str,
    etd: datetime,
    eta: datetime | None,
    quote: date,
    validity: int,
    invoice: date | None,
    bol: date | None,
    status: StartStatus = S.QUOTED,
) -> ShipmentCreate:
    return ShipmentCreate(
        container_id=container_id,
        container_type=container_type,
        status=status,
        cargo_type=cargo,
        hs_code=hs_code,
        declared_qty=Decimal(qty),
        declared_value=Decimal(value),
        declared_currency=currency,
        origin_port=origin,
        destination_port="INNSA",
        etd=etd,
        eta=eta,
        quote_date=quote,
        quote_validity_days=validity,
        invoice_date=invoice,
        bol_date=bol,
    )


def _doc(
    doc_type: DocType, issued: date, qty: int, value: str | None = None, currency: str | None = None
) -> DocumentCreate:
    return DocumentCreate(
        doc_type=doc_type,
        issued_date=issued,
        qty=Decimal(qty),
        value=None if value is None else Decimal(value),
        currency=currency,
    )


def _papers(
    qty: int, value: str, currency: str, invoice: date, bol: date
) -> tuple[DocumentCreate, ...]:
    """The three core documents, agreeing with each other and with the booking."""
    return (
        _doc(INVOICE, invoice, qty, value, currency),
        _doc(PACKING, invoice, qty),
        _doc(BOL, bol, qty),
    )


WT01 = FixtureShipment(
    key="wt01-straight-through",
    description="Door hinges from Shanghai; papers agree, docked, in free time.",
    shipment=_ship(
        "OOLU8101010",
        ContainerType.DRY_20,
        "Auto parts - door hinges",
        "87082900",
        qty=2400,
        value="36000.00",
        currency="USD",
        origin="CNSHA",
        etd=_utc(9, 6, 10),
        eta=_utc(10, 4, 6),
        quote=date(2026, 8, 28),
        validity=14,
        invoice=date(2026, 9, 1),
        bol=date(2026, 9, 6),
    ),
    documents=_papers(2400, "36000.00", "USD", date(2026, 9, 1), date(2026, 9, 6)),
    status_path=TO_DOCKED,
)

WT02 = FixtureShipment(
    key="wt02-packing-list-short",
    description="Drive axles from Ningbo; the packing list counts 470, the invoice 500.",
    shipment=_ship(
        "EGHU8102020",
        ContainerType.DRY_40,
        "Auto parts - drive axles",
        "87085000",
        qty=500,
        value="145000.00",
        currency="USD",
        origin="CNNGB",
        etd=_utc(9, 4, 8),
        eta=_utc(9, 30, 4),
        quote=date(2026, 8, 20),
        validity=14,
        invoice=date(2026, 8, 30),
        bol=date(2026, 9, 4),
    ),
    documents=(
        _doc(INVOICE, date(2026, 8, 30), 500, "145000.00", "USD"),
        _doc(PACKING, date(2026, 8, 30), 470),
        _doc(BOL, date(2026, 9, 4), 500),
    ),
    status_path=TO_DOCKED,
)

WT03 = FixtureShipment(
    key="wt03-value-differs",
    description="Radiators from Hamburg; the invoice says EUR 64,800, the booking EUR 61,200.",
    shipment=_ship(
        "TGHU8103030",
        ContainerType.HIGH_CUBE_40,
        "Auto parts - radiators",
        "87089100",
        qty=900,
        value="61200.00",
        currency="EUR",
        origin="DEHAM",
        etd=_utc(8, 24, 12),
        eta=_utc(9, 28, 5),
        quote=date(2026, 8, 10),
        validity=14,
        invoice=date(2026, 8, 19),
        bol=date(2026, 8, 24),
    ),
    documents=_papers(900, "64800.00", "EUR", date(2026, 8, 19), date(2026, 8, 24)),
    status_path=TO_DOCKED,
)

WT04 = FixtureShipment(
    key="wt04-wrong-currency",
    description="Clutch kits from Genoa; booked in EUR, the invoice was issued in USD.",
    shipment=_ship(
        "CMAU8104040",
        ContainerType.DRY_20,
        "Auto parts - clutch kits",
        "87089300",
        qty=1500,
        value="52500.00",
        currency="EUR",
        origin="ITGOA",
        etd=_utc(9, 8, 9),
        eta=_utc(10, 1, 7),
        quote=date(2026, 8, 28),
        validity=10,
        invoice=date(2026, 9, 2),
        bol=date(2026, 9, 8),
    ),
    documents=_papers(1500, "52500.00", "USD", date(2026, 9, 2), date(2026, 9, 8)),
    status_path=TO_DOCKED,
)

WT05 = FixtureShipment(
    key="wt05-currency-typo",
    description="Seat belts from Ho Chi Minh City; the invoice currency reads 'USS'.",
    shipment=_ship(
        "HLXU8105050",
        ContainerType.DRY_40,
        "Auto parts - seat belts",
        "87082100",
        qty=3000,
        value="42000.00",
        currency="USD",
        origin="VNSGN",
        etd=_utc(9, 7, 3),
        eta=_utc(10, 3, 8),
        quote=date(2026, 9, 1),
        validity=14,
        invoice=date(2026, 9, 4),
        bol=date(2026, 9, 7),
    ),
    documents=_papers(3000, "42000.00", "USS", date(2026, 9, 4), date(2026, 9, 7)),
    status_path=TO_DOCKED,
)

WT06 = FixtureShipment(
    key="wt06-bill-of-lading-missing",
    description="Exhaust parts from Laem Chabang; docked without a bill of lading on file.",
    shipment=_ship(
        "MSCU8106060",
        ContainerType.HIGH_CUBE_40,
        "Auto parts - silencers and exhaust pipes",
        "87089200",
        qty=1100,
        value="77000.00",
        currency="USD",
        origin="THLCH",
        etd=_utc(9, 5, 6),
        eta=_utc(10, 1, 10),
        quote=date(2026, 8, 25),
        validity=14,
        invoice=date(2026, 9, 1),
        bol=date(2026, 9, 5),
    ),
    documents=_papers(1100, "77000.00", "USD", date(2026, 9, 1), date(2026, 9, 5))[:2],
    status_path=TO_DOCKED,
)

WT07 = FixtureShipment(
    key="wt07-still-in-transit",
    description="Alloy wheels from Shanghai; ETA has passed but the record still says in transit.",
    shipment=_ship(
        "ONEU8107070",
        ContainerType.DRY_40,
        "Auto parts - alloy road wheels",
        "87087000",
        qty=800,
        value="96000.00",
        currency="USD",
        origin="CNSHA",
        etd=_utc(9, 8, 11),
        eta=_utc(10, 3, 2),
        quote=date(2026, 8, 27),
        validity=14,
        invoice=date(2026, 9, 3),
        bol=date(2026, 9, 8),
    ),
    documents=_papers(800, "96000.00", "USD", date(2026, 9, 3), date(2026, 9, 8)),
    status_path=(S.BOOKED, S.IN_TRANSIT),
)

WT08 = FixtureShipment(
    key="wt08-held-by-customs",
    description="Bumpers from Qingdao; papers agree, but customs is holding the box.",
    shipment=_ship(
        "YMLU8108080",
        ContainerType.DRY_20,
        "Auto parts - bumpers",
        "87081000",
        qty=650,
        value="26000.00",
        currency="USD",
        origin="CNTAO",
        etd=_utc(8, 29, 5),
        eta=_utc(9, 25, 6),
        quote=date(2026, 8, 18),
        validity=14,
        invoice=date(2026, 8, 26),
        bol=date(2026, 8, 29),
    ),
    documents=_papers(650, "26000.00", "USD", date(2026, 8, 26), date(2026, 8, 29)),
    status_path=(*TO_DOCKED, S.HELD),
)

WT09 = FixtureShipment(
    key="wt09-reefer-without-tariff",
    description="Airbag modules from Tokyo in a 20ft reefer; INNSA has no reefer tariff yet.",
    shipment=_ship(
        "SEGU8109090",
        ContainerType.REEFER_20,
        "Auto parts - airbag modules (temperature-controlled)",
        "87089500",
        qty=400,
        value="13200000",
        currency="JPY",
        origin="JPTYO",
        etd=_utc(9, 12, 1),
        eta=_utc(10, 2, 3),
        quote=date(2026, 9, 5),
        validity=14,
        invoice=date(2026, 9, 10),
        bol=date(2026, 9, 12),
    ),
    documents=_papers(400, "13200000", "JPY", date(2026, 9, 10), date(2026, 9, 12)),
    status_path=TO_DOCKED,
)

WT10 = FixtureShipment(
    key="wt10-no-arrival-date",
    description="Shock absorbers from Mersin; docked, but nobody recorded the arrival.",
    shipment=_ship(
        "ZIMU8110100",
        ContainerType.DRY_40,
        "Auto parts - shock absorbers",
        "87088000",
        qty=1000,
        value="54000.00",
        currency="USD",
        origin="TRMER",
        etd=_utc(9, 2, 7),
        eta=None,
        quote=date(2026, 8, 24),
        validity=14,
        invoice=date(2026, 8, 28),
        bol=date(2026, 9, 2),
    ),
    documents=_papers(1000, "54000.00", "USD", date(2026, 8, 28), date(2026, 9, 2)),
    status_path=TO_DOCKED,
)

WT11 = FixtureShipment(
    key="wt11-new-rule-waits",
    description="LED headlamps from Shenzhen (heading 8512); a new lamp rule is on its way.",
    shipment=_ship(
        "BMOU8111110",
        ContainerType.HIGH_CUBE_40,
        "Vehicle lighting - LED headlamp assemblies",
        "85122010",
        qty=2000,
        value="118000.00",
        currency="USD",
        origin="CNSZX",
        etd=_utc(9, 11, 4),
        eta=_utc(10, 3, 9),
        quote=date(2026, 9, 3),
        validity=14,
        invoice=date(2026, 9, 8),
        bol=date(2026, 9, 11),
    ),
    documents=_papers(2000, "118000.00", "USD", date(2026, 9, 8), date(2026, 9, 11)),
    status_path=TO_DOCKED,
)

WT12 = FixtureShipment(
    key="wt12-filing-rejected-once",
    description="Gearbox housings from Busan; no Korean certificate of origin on file.",
    shipment=_ship(
        "TCNU8112120",
        ContainerType.DRY_40,
        "Auto parts - gearbox housings",
        "87084000",
        qty=700,
        value="119000.00",
        currency="USD",
        origin="KRPUS",
        etd=_utc(9, 6, 2),
        eta=_utc(10, 2, 6),
        quote=date(2026, 8, 26),
        validity=14,
        invoice=date(2026, 9, 1),
        bol=date(2026, 9, 6),
    ),
    documents=_papers(700, "119000.00", "USD", date(2026, 9, 1), date(2026, 9, 6)),
    status_path=TO_DOCKED,
)

WT13 = FixtureShipment(
    key="wt13-papers-change-while-waiting",
    description="Brake pads from Ningbo; the certificate of conformity arrives late.",
    shipment=_ship(
        "FCIU8113130",
        ContainerType.DRY_20,
        "Auto parts - brake pads and discs",
        "87083000",
        qty=5000,
        value="47500.00",
        currency="USD",
        origin="CNNGB",
        etd=_utc(9, 9, 6),
        eta=_utc(10, 4, 1),
        quote=date(2026, 8, 30),
        validity=14,
        invoice=date(2026, 9, 4),
        bol=date(2026, 9, 9),
    ),
    documents=_papers(5000, "47500.00", "USD", date(2026, 9, 4), date(2026, 9, 9)),
    status_path=TO_DOCKED,
)

WT14 = FixtureShipment(
    key="wt14-quote-to-clearance",
    description="Steering wheels from Shanghai; only quoted, and the quote has expired.",
    shipment=_ship(
        "GESU8114140",
        ContainerType.DRY_40,
        "Auto parts - steering wheels",
        "87089400",
        qty=1200,
        value="66000.00",
        currency="USD",
        origin="CNSHA",
        etd=_utc(9, 20, 8),
        eta=_utc(10, 4, 5),
        quote=date(2026, 9, 15),
        validity=7,
        invoice=None,
        bol=None,
        status=S.QUOTED,
    ),
    documents=(),
)

WT15 = FixtureShipment(
    key="wt15-to-do-items-only",
    description="Wiper systems from Xiamen; small date and rate loose ends, nothing blocking.",
    shipment=_ship(
        "TRHU8115150",
        ContainerType.DRY_20,
        "Auto parts - wiper systems",
        "87089900",
        qty=3200,
        value="28800.00",
        currency="USD",
        origin="CNXMN",
        etd=_utc(9, 10, 5),
        eta=_utc(10, 4, 7),
        quote=date(2026, 8, 25),
        validity=14,
        invoice=date(2026, 9, 2),
        bol=date(2026, 9, 10),
    ),
    documents=(
        _doc(INVOICE, date(2026, 9, 3), 3200, "28800.00", "USD"),  # booking says 2 Sep
        _doc(PACKING, date(2026, 9, 2), 3200),
        _doc(BOL, date(2026, 9, 10), 3200),
    ),
    status_path=TO_DOCKED,
)

WALKTHROUGH: tuple[FixtureShipment, ...] = (
    WT01,
    WT02,
    WT03,
    WT04,
    WT05,
    WT06,
    WT07,
    WT08,
    WT09,
    WT10,
    WT11,
    WT12,
    WT13,
    WT14,
    WT15,
)


def _lock(pair: str, lock_type: LockType, day: date, rate: str) -> LockedRateCreate:
    return LockedRateCreate(pair=pair, lock_type=lock_type, lock_date=day, rate=Decimal(rate))


INV, ONBOARD = LockType.COMMERCIAL_INVOICE, LockType.BOL

# The rates locked on each case's invoice and bill of lading dates (a shared date is locked
# once). None for WT14 (the guide has you lock them) or WT15 (left missing on purpose).
WALKTHROUGH_LOCKS: tuple[LockedRateCreate, ...] = (
    _lock("USD/INR", INV, date(2026, 8, 26), "87.930000"),  # WT08
    _lock("USD/INR", ONBOARD, date(2026, 8, 29), "87.975000"),  # WT08
    _lock("USD/INR", INV, date(2026, 8, 28), "87.960000"),  # WT10
    _lock("USD/INR", ONBOARD, date(2026, 9, 2), "88.020000"),  # WT10
    _lock("USD/INR", INV, date(2026, 8, 30), "87.990000"),  # WT02
    _lock("USD/INR", ONBOARD, date(2026, 9, 4), "88.040000"),  # WT02
    _lock("USD/INR", INV, date(2026, 9, 1), "88.015000"),  # WT01, WT06, WT12
    _lock("USD/INR", ONBOARD, date(2026, 9, 6), "88.082000"),  # WT01, WT12
    _lock("USD/INR", ONBOARD, date(2026, 9, 5), "88.060000"),  # WT06
    _lock("USD/INR", INV, date(2026, 9, 3), "88.030000"),  # WT07
    _lock("USD/INR", ONBOARD, date(2026, 9, 8), "88.110000"),  # WT07
    _lock("USD/INR", INV, date(2026, 9, 4), "88.040000"),  # WT05, WT13
    _lock("USD/INR", ONBOARD, date(2026, 9, 7), "88.095000"),  # WT05
    _lock("USD/INR", ONBOARD, date(2026, 9, 9), "88.125000"),  # WT13
    _lock("USD/INR", INV, date(2026, 9, 8), "88.110000"),  # WT11
    _lock("USD/INR", ONBOARD, date(2026, 9, 11), "88.150000"),  # WT11
    _lock("EUR/INR", INV, date(2026, 8, 19), "102.480000"),  # WT03
    _lock("EUR/INR", ONBOARD, date(2026, 8, 24), "102.610000"),  # WT03
    _lock("EUR/INR", INV, date(2026, 9, 2), "102.990000"),  # WT04
    _lock("EUR/INR", ONBOARD, date(2026, 9, 8), "103.120000"),  # WT04
    _lock("JPY/INR", INV, date(2026, 9, 10), "0.602800"),  # WT09
    _lock("JPY/INR", ONBOARD, date(2026, 9, 12), "0.601100"),  # WT09
)

# WT11: the section you add to the Customs Bulletin (Edit tab) before the scan.
LAMP_RULE_KEY = "HSN-8512-lamp-approval"
LAMP_RULE_SECTION = """\
## HSN-8512-lamp-approval
Effective: 2026-10-01
HS codes: 8512
Lighting and signalling equipment for motor vehicles (heading 8512) is cleared only against a
type-approval certificate from a notified testing agency for each lamp model on the invoice.
Consignments without it are examined before assessment.
"""

# WT09: the tariff you add on the Demurrage page (it must already apply on the arrival day).
REEFER_TARIFF = {
    "port_code": "INNSA",
    "container_type": ContainerType.REEFER_20,
    "currency": "USD",
    "free_days": 3,
    "daily_rate": Decimal("95.00"),
    "effective_from": date(2026, 1, 1),
    "effective_to": None,
}

# WT14: the rates you lock on FX Rates -> Locked for the dates you give the booking.
WT14_LOCKS: tuple[LockedRateCreate, ...] = (
    _lock("USD/INR", INV, date(2026, 9, 18), "88.210000"),
    _lock("USD/INR", ONBOARD, date(2026, 9, 20), "88.260000"),
)

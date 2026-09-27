"""The four seed shipments of the JNPT auto-parts case study, plus a factory for ad-hoc ones.

Each fixture exercises one scenario the agents must tell apart:

* ``clean``             — documents agree; nothing should be flagged.
* ``qty_mismatch``      — invoice says 800 gearboxes, packing list says 760.
* ``missing_document``  — no commercial invoice attached.
* ``already_delayed``   — docked 2026-09-08 and held; demurrage is accruing.

Dates are fixed (not relative to "today") so FX and demurrage lookups in later phases are
reproducible (problem statement §3, determinism). All ship to INNSA (Nhava Sheva / JNPT) under
HS heading 8708 (parts and accessories of motor vehicles).
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from models.enums import DocType, ShipmentStatus
from models.schemas import DocumentCreate, ShipmentCreate

S = ShipmentStatus


@dataclass(frozen=True)
class FixtureShipment:
    """A shipment to create, the documents to attach, and the status moves that follow."""

    key: str
    description: str
    shipment: ShipmentCreate
    documents: tuple[DocumentCreate, ...]
    status_path: tuple[ShipmentStatus, ...] = field(default=())


def _utc(y: int, m: int, d: int, hh: int = 0) -> datetime:
    return datetime(y, m, d, hh, tzinfo=UTC)


def _doc(doc_type: DocType, issued: date, **kw: Any) -> DocumentCreate:
    return DocumentCreate(doc_type=doc_type, issued_date=issued, **kw)


TO_DOCKED = (S.BOOKED, S.IN_TRANSIT, S.DOCKED)

CLEAN = FixtureShipment(
    key="clean",
    description="Brake assemblies from Shanghai; all documents consistent.",
    shipment=ShipmentCreate(
        container_id="MSKU7001001",
        cargo_type="Auto parts - brake assemblies",
        hs_code="87083000",
        declared_qty=Decimal("1200"),
        declared_value=Decimal("84000.00"),
        declared_currency="USD",
        origin_port="CNSHA",
        destination_port="INNSA",
        etd=_utc(2026, 8, 22, 10),
        eta=_utc(2026, 9, 20, 6),
        quote_date=date(2026, 8, 10),
        quote_validity_days=14,
        bol_date=date(2026, 8, 22),
        invoice_date=date(2026, 8, 18),
    ),
    documents=(
        _doc(
            DocType.INVOICE,
            date(2026, 8, 18),
            qty=Decimal("1200"),
            value=Decimal("84000.00"),
            currency="USD",
        ),
        _doc(DocType.PACKING_LIST, date(2026, 8, 18), qty=Decimal("1200")),
        _doc(DocType.BILL_OF_LADING, date(2026, 8, 22), qty=Decimal("1200")),
    ),
    status_path=TO_DOCKED,
)

QTY_MISMATCH = FixtureShipment(
    key="qty_mismatch",
    description="Gearboxes from Busan; packing list is 40 units short of the invoice.",
    shipment=ShipmentCreate(
        container_id="MSCU7002002",
        cargo_type="Auto parts - gearboxes",
        hs_code="87084000",
        declared_qty=Decimal("800"),
        declared_value=Decimal("136000.00"),
        declared_currency="USD",
        origin_port="KRPUS",
        destination_port="INNSA",
        etd=_utc(2026, 8, 25, 14),
        eta=_utc(2026, 9, 22, 8),
        quote_date=date(2026, 8, 12),
        quote_validity_days=10,
        bol_date=date(2026, 8, 25),
        invoice_date=date(2026, 8, 20),
    ),
    documents=(
        _doc(
            DocType.INVOICE,
            date(2026, 8, 20),
            qty=Decimal("800"),
            value=Decimal("136000.00"),
            currency="USD",
        ),
        _doc(DocType.PACKING_LIST, date(2026, 8, 20), qty=Decimal("760")),
        _doc(DocType.BILL_OF_LADING, date(2026, 8, 25), qty=Decimal("800")),
    ),
    status_path=TO_DOCKED,
)

MISSING_DOCUMENT = FixtureShipment(
    key="missing_document",
    description="Steering components from Yokohama; commercial invoice not yet attached.",
    shipment=ShipmentCreate(
        container_id="CMAU7003003",
        cargo_type="Auto parts - steering columns",
        hs_code="87089400",
        declared_qty=Decimal("450"),
        declared_value=Decimal("18500000"),
        declared_currency="JPY",
        origin_port="JPYOK",
        destination_port="INNSA",
        etd=_utc(2026, 9, 10, 2),
        eta=_utc(2026, 10, 5, 4),
        quote_date=date(2026, 9, 1),
        quote_validity_days=7,
        bol_date=date(2026, 9, 10),
    ),
    documents=(
        _doc(DocType.PACKING_LIST, date(2026, 9, 8), qty=Decimal("450")),
        _doc(DocType.BILL_OF_LADING, date(2026, 9, 10), qty=Decimal("450")),
    ),
    status_path=(S.BOOKED, S.IN_TRANSIT),
)

ALREADY_DELAYED = FixtureShipment(
    key="already_delayed",
    description="Suspension parts from Hamburg; docked 2026-09-08 and held at customs.",
    shipment=ShipmentCreate(
        container_id="HLXU7004004",
        cargo_type="Auto parts - suspension systems",
        hs_code="87088000",
        declared_qty=Decimal("600"),
        declared_value=Decimal("96500.00"),
        declared_currency="EUR",
        origin_port="DEHAM",
        destination_port="INNSA",
        etd=_utc(2026, 8, 1, 12),
        eta=_utc(2026, 9, 8, 5),
        quote_date=date(2026, 7, 20),
        quote_validity_days=14,
        bol_date=date(2026, 8, 1),
        invoice_date=date(2026, 7, 28),
    ),
    documents=(
        _doc(
            DocType.INVOICE,
            date(2026, 7, 28),
            qty=Decimal("600"),
            value=Decimal("96500.00"),
            currency="EUR",
        ),
        _doc(DocType.PACKING_LIST, date(2026, 7, 28), qty=Decimal("600")),
        _doc(DocType.BILL_OF_LADING, date(2026, 8, 1), qty=Decimal("600")),
    ),
    status_path=(*TO_DOCKED, S.UNDER_REVIEW, S.HELD),
)

SEED_SHIPMENTS: tuple[FixtureShipment, ...] = (
    CLEAN,
    QTY_MISMATCH,
    MISSING_DOCUMENT,
    ALREADY_DELAYED,
)


def make_shipment(**overrides: Any) -> ShipmentCreate:
    """A valid ``ShipmentCreate`` for ad-hoc tests; override any field by keyword."""
    base: dict[str, Any] = CLEAN.shipment.model_dump(exclude_unset=True)
    base.update(container_id="TSTU0000001")
    base.update(overrides)
    return ShipmentCreate(**base)

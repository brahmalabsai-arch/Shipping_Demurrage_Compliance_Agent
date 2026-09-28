"""Domain enumerations shared by the ORM (Postgres ENUM types) and the Pydantic schemas."""

from enum import StrEnum


class ShipmentStatus(StrEnum):
    """Shipment lifecycle. ``quoted`` precedes ``booked`` (problem statement §5).

    ``cancelled`` is terminal; it exists so an in-flight agent pipeline can detect a
    cancellation at read time and stop (edge cases, Phase 1).
    """

    QUOTED = "quoted"
    BOOKED = "booked"
    IN_TRANSIT = "in_transit"
    DOCKED = "docked"
    UNDER_REVIEW = "under_review"
    CLEARED = "cleared"
    HELD = "held"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ContainerType(StrEnum):
    """ISO container sizes/types that port tariffs price separately (stored as TEXT)."""

    DRY_20 = "20ft"
    DRY_40 = "40ft"
    HIGH_CUBE_40 = "40hc"
    REEFER_20 = "20rf"
    REEFER_40 = "40rf"


class DocType(StrEnum):
    """Kinds of shipment document that verification compares against each other."""

    INVOICE = "invoice"
    PACKING_LIST = "packing_list"
    BILL_OF_LADING = "bill_of_lading"
    CERTIFICATE = "certificate"
    OTHER = "other"


class FxStage(StrEnum):
    """Shipment lifecycle stage an FX rate is asked for; each has its own rule (arch §8)."""

    QUOTATION = "quotation"
    BOL = "bol"
    INVOICE = "invoice"
    PORT_CHARGES = "port_charges"


class LockType(StrEnum):
    """Event that locks a historical FX rate (``fx.locked_rates.lock_type``)."""

    BOL = "bol"
    COMMERCIAL_INVOICE = "commercial_invoice"


class InvoiceKind(StrEnum):
    """Invoice-stage variants: a commercial invoice locks to its own date, a freight invoice
    re-uses the rate locked on the bill of lading date."""

    COMMERCIAL = "commercial"
    FREIGHT = "freight"


class RateSource(StrEnum):
    """Which ``fx.*`` table a resolved rate came from."""

    SPOT = "spot"
    LOCKED = "locked"
    GAZETTED = "gazetted"


class RuleStatus(StrEnum):
    """Compliance rule review state. Every rule is created ``pending``; only an explicit human
    decision (``compliance_service.decide``) moves it, once, to ``approved`` or ``rejected``."""

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


class RuleDecision(StrEnum):
    """A reviewer's verdict on a pending compliance rule."""

    APPROVE = "approve"
    REJECT = "reject"


class SectionChangeKind(StrEnum):
    """How one rule section of the customs bulletin changed between two snapshots."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"

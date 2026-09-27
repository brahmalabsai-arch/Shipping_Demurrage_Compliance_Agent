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

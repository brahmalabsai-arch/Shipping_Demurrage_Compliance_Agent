"""Pydantic v2 models — the canonical schema for MCP tool I/O, LangGraph state and FastAPI bodies.

Domain objects are named ``<Noun>Record`` (e.g. ``ShipmentRecord``) to distinguish them from the
SQLAlchemy ``<Noun>`` ORM classes (conventions §2). Input models (``*Create`` / ``*Update``)
validate at the boundary so bad data never reaches the service layer.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from models.enums import DocType, ShipmentStatus

# ISO 4217 *shape* only. Whether a code is a real currency (e.g. 'XYZ' is not) is the document
# verification agent's call — it must flag it rather than have it rejected here (edge cases, Ph 7).
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
# ISO 6346 container number: 4-letter owner/category code + 7 digits.
ContainerId = Annotated[str, Field(pattern=r"^[A-Z]{4}\d{7}$")]
# HS / ITC-HS code, digits only: 4, 6, 8 or 10 digits.
HsCode = Annotated[str, Field(pattern=r"^\d{4}(\d{2}){0,3}$")]
# UN/LOCODE, e.g. INNSA (Nhava Sheva / JNPT).
PortCode = Annotated[str, Field(pattern=r"^[A-Z]{2}[A-Z2-9]{3}$")]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
QuoteValidityDays = Annotated[int, Field(ge=7, le=14)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _check_quote_pair(quote_date: date | None, validity: int | None) -> None:
    if (quote_date is None) != (validity is None):
        raise ValueError("quote_date and quote_validity_days must be set together")


# --------------------------------------------------------------------------- shipments


class ShipmentCreate(_Input):
    """A new shipment. It enters the lifecycle as a quotation or a direct booking only."""

    container_id: ContainerId
    status: Literal[ShipmentStatus.QUOTED, ShipmentStatus.BOOKED] = ShipmentStatus.QUOTED
    cargo_type: Annotated[str, Field(min_length=1)]
    hs_code: HsCode
    declared_qty: NonNegativeDecimal
    declared_value: NonNegativeDecimal
    declared_currency: CurrencyCode
    origin_port: PortCode
    destination_port: PortCode
    etd: AwareDatetime
    eta: AwareDatetime | None = None
    quote_date: date | None = None
    quote_validity_days: QuoteValidityDays | None = None
    bol_date: date | None = None
    invoice_date: date | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_quote_pair(self.quote_date, self.quote_validity_days)
        if self.status is ShipmentStatus.QUOTED and self.quote_date is None:
            raise ValueError("a shipment created as 'quoted' needs quote_date and validity")
        if self.eta is not None and self.eta < self.etd:
            raise ValueError("eta cannot be before etd")
        return self


class ShipmentUpdate(_Input):
    """Partial edit of a shipment's descriptive fields. Only fields explicitly set are applied.

    ``container_id`` (identity) and ``status`` (see ``update_status``) are not editable here.
    Cross-field rules are re-checked by the service against the merged row.
    """

    cargo_type: Annotated[str, Field(min_length=1)] | None = None
    hs_code: HsCode | None = None
    declared_qty: NonNegativeDecimal | None = None
    declared_value: NonNegativeDecimal | None = None
    declared_currency: CurrencyCode | None = None
    origin_port: PortCode | None = None
    destination_port: PortCode | None = None
    etd: AwareDatetime | None = None
    eta: AwareDatetime | None = None
    quote_date: date | None = None
    quote_validity_days: QuoteValidityDays | None = None
    bol_date: date | None = None
    invoice_date: date | None = None

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "cargo_type",
        "hs_code",
        "declared_qty",
        "declared_value",
        "declared_currency",
        "origin_port",
        "destination_port",
        "etd",
    )

    @model_validator(mode="after")
    def _no_nulling_required(self) -> Self:
        nulled = [
            f for f in self._REQUIRED if f in self.model_fields_set and getattr(self, f) is None
        ]
        if nulled:
            raise ValueError(f"cannot clear required field(s): {', '.join(nulled)}")
        return self


class ShipmentRecord(BaseModel):
    """A shipment as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    container_id: str
    status: ShipmentStatus
    cargo_type: str
    hs_code: str
    declared_qty: Decimal
    declared_value: Decimal
    declared_currency: str
    origin_port: str
    destination_port: str
    etd: datetime
    eta: datetime | None
    quote_date: date | None
    quote_validity_days: int | None
    quote_expiry_date: date | None
    bol_date: date | None
    invoice_date: date | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- documents


class DocumentCreate(_Input):
    """A newly attached document (always version 1 of its own chain)."""

    doc_type: DocType
    qty: NonNegativeDecimal | None = None
    value: NonNegativeDecimal | None = None
    currency: CurrencyCode | None = None
    issued_date: date


class DocumentAmend(_Input):
    """Changes for a re-issued document; unset fields carry over from the version it replaces."""

    qty: NonNegativeDecimal | None = None
    value: NonNegativeDecimal | None = None
    currency: CurrencyCode | None = None
    issued_date: date | None = None

    @model_validator(mode="after")
    def _issued_date_not_cleared(self) -> Self:
        if "issued_date" in self.model_fields_set and self.issued_date is None:
            raise ValueError("cannot clear required field: issued_date")
        return self


class DocumentRecord(BaseModel):
    """A document version as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    shipment_id: UUID
    doc_type: DocType
    version: int
    qty: Decimal | None
    value: Decimal | None
    currency: str | None
    issued_date: date
    superseded_by: UUID | None
    created_at: datetime

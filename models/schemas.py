"""Pydantic v2 models — the canonical schema for MCP tool I/O, LangGraph state and FastAPI bodies.

Domain objects are named ``<Noun>Record`` (e.g. ``ShipmentRecord``) to distinguish them from the
SQLAlchemy ``<Noun>`` ORM classes (conventions §2). Input models (``*Create`` / ``*Update``)
validate at the boundary so bad data never reaches the service layer.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from models.enums import (
    ContainerType,
    DocType,
    FxStage,
    InvoiceKind,
    LockType,
    RateSource,
    RuleStatus,
    SectionChangeKind,
    ShipmentStatus,
)

# Deliberately loose (shape only): the mail backend is the authority on deliverability.
EmailAddress = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
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
# 'BASE/QUOTE', e.g. 'USD/INR': 1 USD = <rate> INR.
CurrencyPair = Annotated[str, Field(pattern=r"^[A-Z]{3}/[A-Z]{3}$")]
# Matches the NUMERIC(12,6) columns, so a value that would be rounded by Postgres is rejected.
FxRateValue = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _check_distinct_currencies(pair: str) -> None:
    base, quote = pair.split("/")
    if base == quote:
        raise ValueError(f"pair {pair} converts a currency into itself")


def _check_quote_pair(quote_date: date | None, validity: int | None) -> None:
    if (quote_date is None) != (validity is None):
        raise ValueError("quote_date and quote_validity_days must be set together")


# --------------------------------------------------------------------------- shipments


class ShipmentCreate(_Input):
    """A new shipment. It enters the lifecycle as a quotation or a direct booking only."""

    container_id: ContainerId
    container_type: ContainerType  # picks the demurrage tariff; required — never assumed
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

    container_type: ContainerType | None = None
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
        "container_type",
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
    container_type: ContainerType
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


# --------------------------------------------------------------------------- demurrage


class PortRateUpsert(_Input):
    """A port tariff to add, or to correct if one already starts on the same day.

    ``effective_to`` is inclusive. Leave it empty to run until the next scheduled period (or
    open-ended if there is none). ``ge=0`` rejects negative rates/free days here, before they
    reach the service (edge cases, Phase 2); ``free_days=0`` is valid (charges from day 1).
    """

    port_code: PortCode
    container_type: ContainerType
    currency: CurrencyCode
    free_days: Annotated[int, Field(ge=0)]
    daily_rate: NonNegativeDecimal
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _valid_period(self) -> Self:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot be before effective_from")
        return self


class PortRateRecord(BaseModel):
    """A port tariff period as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    port_code: str
    container_type: ContainerType
    currency: str
    free_days: int
    daily_rate: Decimal
    effective_from: date
    effective_to: date | None


class DemurrageCharge(BaseModel):
    """Charge for a stay at port under one tariff, with the figures it was built from."""

    model_config = ConfigDict(frozen=True)

    rate: PortRateRecord
    days_in_port: int
    chargeable_days: int
    amount: Decimal
    currency: str


# --------------------------------------------------------------------------- fx


class SpotRateUpsert(_Input):
    """A spot observation to add, or to correct if one exists for the same pair and instant."""

    pair: CurrencyPair
    rate: FxRateValue
    as_of: AwareDatetime
    source: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _check_distinct_currencies(self.pair)
        return self


class SpotRateRecord(BaseModel):
    """A spot observation as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    pair: str
    rate: Decimal
    as_of: datetime
    source: str


class LockedRateCreate(_Input):
    """A rate to lock on a bill-of-lading or commercial-invoice date. Cannot be changed later."""

    pair: CurrencyPair
    rate: FxRateValue
    lock_date: date
    lock_type: LockType

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _check_distinct_currencies(self.pair)
        return self


class LockedRateRecord(BaseModel):
    """A locked rate as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    pair: str
    rate: Decimal
    lock_date: date
    lock_type: LockType


class GazettedRateCreate(_Input):
    """A gazetted notification for a period (inclusive), or — with ``revision_of`` — a correction
    of an existing row covering all or part of that row's period."""

    pair: CurrencyPair
    rate: FxRateValue
    period_start: date
    period_end: date
    publishing_authority: Annotated[str, Field(min_length=1)]
    revision_of: UUID | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_distinct_currencies(self.pair)
        if self.period_end < self.period_start:
            raise ValueError("period_end cannot be before period_start")
        return self


class GazettedRateRecord(BaseModel):
    """A gazetted rate row as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    pair: str
    rate: Decimal
    period_start: date
    period_end: date
    publishing_authority: str
    revision_of: UUID | None


class ResolvedRate(BaseModel):
    """The FX rate that applies to one (pair, date, stage), and exactly where it came from.

    ``source_id`` is the ``fx.*`` row used, so an agent's conversion can be traced back to the
    data it was built from. ``locked`` is True only for rates from ``fx.locked_rates`` (which can
    never change). ``expires`` is set for quotations only: the quote is valid through that day.
    """

    model_config = ConfigDict(frozen=True)

    pair: str
    stage: FxStage
    as_of: date
    rate: Decimal
    source: RateSource
    source_id: UUID
    basis: str
    locked: bool
    expires: date | None = None
    invoice_kind: InvoiceKind | None = None


# --------------------------------------------------------------------------- compliance

# Stable identifier of a rule across versions, e.g. 'HSN-8708-duty'.
RuleKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$", max_length=80)]


class RuleProposal(_Input):
    """A proposed new version of a compliance rule. It is always stored ``pending``.

    ``hs_codes`` empty means the rule applies to every HS code. ``source_diff_id`` links a
    proposal raised from a detected bulletin change to its ``bulletin.change_log`` entry.
    """

    rule_key: RuleKey
    content: Annotated[str, Field(min_length=1)]
    hs_codes: list[HsCode] = Field(default_factory=list)
    effective_date: date
    change_summary: str | None = None
    source_diff_id: UUID | None = None

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        self.hs_codes = sorted(set(self.hs_codes))
        if self.change_summary is not None and not self.change_summary:
            self.change_summary = None
        return self


class ComplianceRuleRecord(BaseModel):
    """A compliance rule version as returned across the service boundary."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    rule_key: str
    version: int
    status: RuleStatus
    content: str
    hs_codes: list[str]
    effective_date: date
    change_summary: str | None
    superseded_by: UUID | None
    source_diff_id: UUID | None
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None

    def applies_to(self, hs_code: str) -> bool:
        """True if the rule covers ``hs_code`` (see :func:`hs_codes_cover`)."""
        return hs_codes_cover(self.hs_codes, hs_code)


def hs_codes_cover(rule_codes: Sequence[str], hs_code: str) -> bool:
    """True if a rule with ``rule_codes`` covers ``hs_code`` — a heading covers its sub-headings
    and vice versa; a rule with no HS codes applies to all goods. Shared by the rulebook reads
    and the RAG index filter, so both always agree."""
    return not rule_codes or any(
        hs_code.startswith(code) or code.startswith(hs_code) for code in rule_codes
    )


# --------------------------------------------------------------------------- bulletin


class BulletinPublicationRecord(BaseModel):
    """The simulated customs bulletin as currently published."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    content: str
    updated_at: datetime
    updated_by: str


class BulletinSnapshotRecord(BaseModel):
    """The bulletin text as captured by one scheduled scan."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    seq: int
    content: str
    captured_at: datetime


class ChangeLogEntryRecord(BaseModel):
    """A detected change between two consecutive snapshots."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    prior_snapshot_id: UUID
    new_snapshot_id: UUID
    diff_summary: str
    is_substantive: bool
    detected_at: datetime
    emailed_at: datetime | None


class BulletinSection(BaseModel):
    """One rule section of the bulletin: ``## <rule_key>``, its metadata lines and its text."""

    model_config = ConfigDict(frozen=True)

    rule_key: str
    effective_date: date
    hs_codes: tuple[str, ...]
    text: str


class SectionChange(BaseModel):
    """How one rule section differs between two bulletin versions.

    ``is_substantive`` is False when only whitespace or letter case changed.
    """

    model_config = ConfigDict(frozen=True)

    rule_key: str
    kind: SectionChangeKind
    before: BulletinSection | None
    after: BulletinSection | None
    is_substantive: bool


class RuleMatch(BaseModel):
    """An approved rule returned by ``compliance_service.search_rules``, with its relevance.

    ``method`` records which retriever produced the score (``keyword`` until the Phase 9
    hybrid retriever replaces it), so results stay interpretable across the switch.
    """

    model_config = ConfigDict(frozen=True)

    rule: ComplianceRuleRecord
    score: float
    method: str


# --------------------------------------------------------------------------- notifications


class EmailReceipt(BaseModel):
    """Proof that a review email was handed to the mail backend."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    backend: str
    to: str
    subject: str
    rule_id: UUID
    sent_at: datetime


# --------------------------------------------------------------------------- review context


class RuleDrift(BaseModel):
    """A rule section of the latest scanned bulletin that differs (beyond whitespace/case) from
    the approved rule in force — or has no approved rule yet (``in_force`` None).

    ``pending_same`` / ``rejected_same`` are existing versions with that very text: the watch
    agent proposes only when both are None, so a pending change is not duplicated and a
    rejected one is never raised again.
    """

    model_config = ConfigDict(frozen=True)

    rule_key: str
    section: BulletinSection
    in_force: ComplianceRuleRecord | None
    pending_same: ComplianceRuleRecord | None = None
    rejected_same: ComplianceRuleRecord | None = None


class ReviewContext(BaseModel):
    """What a reviewer needs to decide on ``proposal``: the rule it would replace and what
    happened in between (other proposals since that version — pending, or rejected and why)."""

    model_config = ConfigDict(frozen=True)

    proposal: ComplianceRuleRecord
    in_force: ComplianceRuleRecord | None
    others: list[ComplianceRuleRecord]
    history: list[str]

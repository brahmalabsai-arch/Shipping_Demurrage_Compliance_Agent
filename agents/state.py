"""LangGraph state schemas.

``WatchState`` is the state of the regulatory watch pipeline (architecture §6: triggered by the
scheduler, not by a shipment). ``CaseState`` is the shipment pipeline's state; each agent of that
pipeline adds its own slice as it is built (Phase 7: the document verification fields).
"""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from models.schemas import (
    ChangeLogEntryRecord,
    ComplianceRuleRecord,
    DocumentRecord,
    EmailReceipt,
    ResolvedRate,
    ShipmentRecord,
)

WatchOutcome = Literal[
    "no_change",  # no new difference between the last two scans
    "formatting_only",  # a difference, but only whitespace/case: nothing to review
    "proposed",  # at least one pending proposal was raised
    "nothing_to_propose",  # substantive, but everything was already pending/rejected/removed
]


class SectionSummary(BaseModel):
    """The plain-language explanation of one changed rule section."""

    rule_key: str
    text: str
    method: Literal["llm", "template"]
    fallback_reason: str | None = None


class WatchState(BaseModel):
    """What one run of the regulatory watch agent found and did."""

    run_id: str
    change: ChangeLogEntryRecord | None = None
    summaries: list[SectionSummary] = Field(default_factory=list)
    proposals: list[ComplianceRuleRecord] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # "<rule_key>: <why not proposed>"
    removed_sections: list[str] = Field(default_factory=list)
    emails: list[EmailReceipt] = Field(default_factory=list)
    email_failures: list[str] = Field(default_factory=list)
    outcome: WatchOutcome | None = None


# --------------------------------------------------------------------------- shipment pipeline

DiscrepancyCode = Literal[
    "missing_document",  # a document the shipment's stage requires is not attached
    "quantity_mismatch",  # documents / shipment record disagree on the quantity
    "value_mismatch",  # the invoice value disagrees with the declared value
    "currency_mismatch",  # documents / shipment record name different currencies
    "invalid_currency",  # not an ISO 4217 currency code (never sent to FX resolution)
    "date_mismatch",  # a document's issue date differs from the date the FX rate locks to
    "quote_expired",  # the quotation's validity window had passed when it was acted on
    "fx_rate_unavailable",  # no FX rate for a lifecycle date (raise, don't guess)
]


class ObservedValue(BaseModel):
    """One value of a checked field, and exactly where it was read."""

    source: str  # "commercial invoice", "packing list", ..., or "shipment record"
    value: str
    document_id: UUID | None = None  # None for the shipment record
    version: int | None = None


class Discrepancy(BaseModel):
    """One field-level finding: which field, every value seen, and by how much they differ."""

    code: DiscrepancyCode
    field: str  # e.g. "qty", "value", "currency", "doc_type", "issued_date", "fx_rate"
    message: str
    observed: list[ObservedValue] = Field(default_factory=list)
    delta: Decimal | None = None  # largest minus smallest value, for numeric fields
    agent: str  # the agent that raised it (the clearance checklist attributes by this)


class FxContext(BaseModel):
    """The FX rates a shipment's documents convert at, resolved per lifecycle stage."""

    pair: str | None = None  # "<currency>/INR"; None when no conversion was attempted
    rates: list[ResolvedRate] = Field(default_factory=list)  # quotation, bol, invoice
    quote_expires: date | None = None
    quote_checked_on: date | None = None  # the date the quote had to be valid on, if any
    quote_expired: bool | None = None  # None = not evaluated (see quote_checked_on)
    skipped_reason: str | None = None  # why no rate was resolved at all


class Explanation(BaseModel):
    """A plain-language explanation of an agent's findings, for the operator."""

    text: str
    method: Literal["llm", "template"]
    fallback_reason: str | None = None


class CaseState(BaseModel):
    """What the shipment pipeline knows about one shipment (architecture §6).

    Inputs: ``run_id``, ``shipment_id`` and ``as_of`` (the analysis date — "today" in IST for a
    live run, fixed in tests). Document verification (Phase 7) writes ``shipment``,
    ``documents``, ``fx_context``, ``document_discrepancies``, ``document_outcome`` and
    ``document_explanation``.
    """

    run_id: str
    shipment_id: UUID
    as_of: date
    shipment: ShipmentRecord | None = None
    documents: list[DocumentRecord] = Field(default_factory=list)  # current versions only
    fx_context: FxContext | None = None
    document_discrepancies: list[Discrepancy] = Field(default_factory=list)
    document_outcome: Literal["clear", "flagged"] | None = None
    document_explanation: Explanation | None = None

"""LangGraph state schemas.

``WatchState`` is the state of the regulatory watch pipeline (architecture §6: triggered by the
scheduler, not by a shipment). ``CaseState`` is the shipment pipeline's state; each agent of that
pipeline adds its own slice as it is built (Phase 7: document verification; Phase 8: demurrage
and risk; ...; Phase 11: the supervisor's human gates).
"""

import operator
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from models.enums import ContainerType, RuleStatus
from models.schemas import (
    ChangeLogEntryRecord,
    ComplianceRuleRecord,
    DocumentRecord,
    EmailReceipt,
    FilingDecisionRecord,
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


RiskLevel = Literal[
    "none",  # cleared or closed: no further port charges accrue
    "low",  # not yet charging and nothing suggests it will
    "medium",  # a hold looks likely (document problems, free time nearly over, ETA passed)
    "high",  # demurrage is accruing, or customs is holding the container
    "indeterminate",  # cannot be assessed (no ETA, no tariff) — never reported as zero risk
]


class DemurrageEstimate(BaseModel):
    """Every figure behind a demurrage estimate, so it can be recomputed by hand (conventions
    §9). The tariff in force on the arrival day prices the whole stay; the arrival day is day 1;
    the conversion uses the gazetted port-charges rate on the analysis date; money is rounded
    once, half-up to 0.01, at the end."""

    port_code: str
    container_type: ContainerType
    arrival_date: date  # IST day of the ETA (the shipments table has no separate dock date)
    arrived: bool
    as_of: date  # the analysis date
    days_in_port: int  # arrival day counts; 0 before arrival
    free_days: int
    charges_from: date  # the first chargeable day
    chargeable_days: int
    daily_rate: Decimal
    currency: str  # the tariff's currency
    tariff_id: UUID  # the demurrage.port_rates row used
    amount: Decimal  # chargeable_days x daily_rate, tariff currency
    fx: ResolvedRate | None = None  # None: no conversion needed, or no rate (see data_gaps)
    amount_inr: Decimal | None = None
    daily_rate_inr: Decimal | None = None  # what each further chargeable day costs
    basis: str  # the computation, in words


class Citation(BaseModel):
    """An approved rule version an answer relies on, as retrieved (reranker ``score``)."""

    rule_id: UUID
    rule_key: str
    version: int
    effective_date: date
    score: float


class ComplianceFinding(BaseModel):
    """One question put to the approved rulebook, and its grounded answer.

    ``found=False`` means the rulebook in force does not answer it — the answer is then exactly
    "Not found in current rulebook." and nothing is guessed.
    """

    question: str
    hs_code: str | None = None
    found: bool
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    method: Literal["llm", "template"]
    fallback_reason: str | None = None
    agent: str


Severity = Literal[
    "blocker",  # must be resolved before the shipment can be cleared
    "action",  # needs someone to act (urgency, a correction), but does not block clearance
    "info",  # context for the reviewer
]


class ChecklistItem(BaseModel):
    """One thing the clearance reviewer must see, attributed to the agent that raised it."""

    key: str  # stable identity — items are de-duplicated by it
    source_agent: str
    category: Literal["document", "demurrage", "data_gap", "compliance"]
    severity: Severity
    title: str
    detail: str
    guidance: str | None = None  # the rulebook's answer for this item, if one was asked
    references: list[str] = Field(default_factory=list)  # rule citations, tariff / FX rows


class Conflict(BaseModel):
    """Upstream agents disagree about urgency; both signals are kept, neither is picked."""

    description: str
    signals: list[str]  # "<agent>: <what it reported>"


class AgentFailure(BaseModel):
    """An upstream agent that raised or timed out; the pipeline carried on without it."""

    agent: str
    kind: Literal["error", "timeout"]
    error: str


class Checklist(BaseModel):
    """The clearance checklist for one shipment (Phase 10 fan-in)."""

    shipment_id: UUID
    container_id: str | None
    as_of: date
    items: list[ChecklistItem]  # blockers first
    missing_inputs: list[str] = Field(default_factory=list)  # e.g. ["demurrage_risk"]
    conflicts: list[Conflict] = Field(default_factory=list)
    ready_for_clearance: bool  # no blocker, no missing input, no conflict
    summary: str


class GateDecision(BaseModel):
    """How the compliance gate's wait on one pending rule version ended — read from the rulebook
    after a human decided it on the Compliance page (the resume carries no decision itself)."""

    rule_id: UUID
    rule_key: str
    version: int
    outcome: RuleStatus  # approved, rejected (final — never re-queued) or, later, revoked
    reviewer: str | None
    note: str | None
    decided_at: datetime | None


RunOutcome = Literal[
    "filed",  # a reviewer approved the filing and the shipment is `cleared`
    "filing_rejected",  # a reviewer refused the filing — final for this run
    "blocked",  # the checklist is not ready for clearance (or the documents changed since)
    "not_fileable",  # the shipment is not at a stage that can be filed (not docked/under review)
]


class CaseState(BaseModel):
    """What the shipment pipeline knows about one shipment (architecture §6).

    Inputs: ``run_id``, ``shipment_id`` and ``as_of`` (the analysis date — "today" in IST for a
    live run, fixed in tests). Document verification (Phase 7) writes ``shipment``,
    ``documents``, ``fx_context``, ``document_discrepancies``, ``document_outcome`` and
    ``document_explanation``. Demurrage & risk (Phase 8) reads ``document_discrepancies`` (if
    verification ran first) and writes ``demurrage_estimate``, ``risk``, ``risk_reasons`` and
    ``data_gaps``. Compliance RAG (Phase 9) reads ``shipment`` and ``document_discrepancies``
    (from verification) and writes ``compliance_findings``. The clearance checklist (Phase 10)
    reads all of the above plus ``agent_failures`` (written by the graph when an upstream agent
    raises or times out) and writes ``checklist``. The supervisor (Phase 11) writes
    ``pending_rules`` (the compliance gate's current wait), ``compliance_decisions``,
    ``filing_decision`` and, when the run ends, ``outcome`` / ``outcome_detail``; after an
    approved rule change it re-runs the compliance agent and the checklist.
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
    demurrage_estimate: DemurrageEstimate | None = None
    risk: RiskLevel | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)  # what could not be resolved, and why
    compliance_findings: list[ComplianceFinding] = Field(default_factory=list)
    # Written by the graph's guard around each upstream agent; parallel branches append.
    agent_failures: Annotated[list[AgentFailure], operator.add] = Field(default_factory=list)
    checklist: Checklist | None = None
    # Supervisor (Phase 11).
    pending_rules: list[ComplianceRuleRecord] = Field(default_factory=list)  # the current wait
    compliance_decisions: Annotated[list[GateDecision], operator.add] = Field(default_factory=list)
    filing_decision: FilingDecisionRecord | None = None
    outcome: RunOutcome | None = None
    outcome_detail: str | None = None

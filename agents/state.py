"""LangGraph state schemas.

``WatchState`` is the state of the regulatory watch pipeline (architecture §6: triggered by the
scheduler, not by a shipment). The shipment pipeline's ``CaseState`` joins it here in the phases
that build those agents.
"""

from typing import Literal

from pydantic import BaseModel, Field

from models.schemas import ChangeLogEntryRecord, ComplianceRuleRecord, EmailReceipt

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

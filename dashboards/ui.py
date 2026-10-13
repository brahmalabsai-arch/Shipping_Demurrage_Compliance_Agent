"""Presentation helpers shared by dashboard pages: time, numbers, writes, and plain words.

Timestamps are stored in UTC and converted to Asia/Kolkata only here, at the presentation layer,
via ``zoneinfo`` (conventions §3). Every write goes through :func:`run_action`, so each form
commits in one transaction, shows problems inline in plain words, and reruns to show fresh data.
The vocabulary below turns internal names (agent modules, issue codes, run states) into the
words an operator or a demo audience reads; the internal names stay available in details.
"""

from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import ValidationError
from sqlalchemy.orm import Session

from services.db import session_scope
from services.errors import CustomsCopilotError

IST = ZoneInfo("Asia/Kolkata")
_FLASH_KEY = "_flash_message"

ToneName = Literal["ok", "warn", "stop", "info", "quiet"]

# --------------------------------------------------------------------------- vocabulary

AGENT_NAMES: dict[str, str] = {
    "document_verification_agent": "Document check",
    "demurrage_risk_agent": "Demurrage & risk",
    "compliance_rag_agent": "Rulebook",
    "clearance_checklist_agent": "Checklist",
    "regulatory_watch_agent": "Regulatory watch",
    "supervisor": "Supervisor",
}
ISSUE_NAMES: dict[str, str] = {
    "missing_document": "Document missing",
    "quantity_mismatch": "Quantities disagree",
    "value_mismatch": "Values disagree",
    "currency_mismatch": "Currencies disagree",
    "invalid_currency": "Unknown currency",
    "date_mismatch": "Dates disagree",
    "quote_expired": "Quote expired",
    "fx_rate_unavailable": "No FX rate",
}
STATUS_WORDS: dict[str, str] = {
    "quoted": "Quoted",
    "booked": "Booked",
    "in_transit": "In transit",
    "docked": "Docked",
    "under_review": "Under review",
    "held": "Held by customs",
    "cleared": "Cleared",
    "closed": "Closed",
    "cancelled": "Cancelled",
}
RISK_WORDS: dict[str, tuple[str, ToneName]] = {
    "high": ("High risk", "stop"),
    "medium": ("Medium risk", "warn"),
    "low": ("Low risk", "ok"),
    "none": ("No risk", "ok"),
    "indeterminate": ("Risk unknown", "quiet"),
}
RUN_STATE_WORDS: dict[str, tuple[str, ToneName]] = {
    "running": ("Running", "info"),
    "awaiting_compliance": ("Waiting for a rule decision", "warn"),
    "awaiting_filing": ("Waiting for filing approval", "warn"),
    "filed": ("Filed and cleared", "ok"),
    "filing_rejected": ("Filing rejected", "stop"),
    "blocked": ("Blocked by the checklist", "stop"),
    "not_fileable": ("Not at a fileable stage", "quiet"),
    "stopped": ("Stopped (cancelled)", "quiet"),
    "failed": ("Ended (failed or abandoned)", "quiet"),
}
SEVERITY_WORDS: dict[str, str] = {"blocker": "Must fix", "action": "To do", "info": "Note"}
ACTION_WORDS: dict[str, str] = {
    "document_verification.completed": "Papers checked",
    "document_verification.stopped": "Paper check stopped",
    "demurrage_risk.completed": "Demurrage and risk assessed",
    "demurrage_risk.stopped": "Demurrage check stopped",
    "compliance_rag.completed": "Rulebook consulted",
    "compliance_rag.answered": "Rulebook question answered",
    "clearance_checklist.completed": "Checklist built",
    "agent.failed": "An agent did not finish",
    "case_run.started": "Clearance run started",
    "case_run.state_changed": "Clearance run moved on",
    "supervisor.compliance_gate_waiting": "Waiting for a rule decision",
    "supervisor.compliance_approved": "Rule decision received: approved",
    "supervisor.compliance_rejected": "Rule decision received: rejected",
    "supervisor.compliance_revoked": "Rule decision received: revoked",
    "supervisor.resumed": "Clearance run resumed",
    "filing.approved": "Filing approved",
    "filing.rejected": "Filing rejected",
    "shipment.created": "Shipment created",
    "shipment.updated": "Shipment edited",
    "shipment.status_changed": "Shipment moved to a new stage",
    "document.added": "Document attached",
    "document.amended": "Document amended",
    "compliance_rule.proposed": "Rule change proposed",
    "compliance_rule.approved": "Rule change approved",
    "compliance_rule.rejected": "Rule change rejected",
    "compliance_rule.superseded": "Rule replaced by a newer version",
    "compliance_rule.revoked": "Rule approval revoked",
    "compliance_rule.reinstated": "Earlier rule back in force",
    "bulletin.published": "Bulletin published",
    "bulletin_snapshot.captured": "Bulletin scanned",
    "bulletin_change.detected": "Bulletin change detected",
    "bulletin_change.emailed": "Reviewer emailed about a change",
    "notification.review_email_sent": "Review email sent",
    "regulatory_watch.completed": "Regulatory watch finished",
    "regulatory_watch.failed": "Regulatory watch failed",
    "regulatory_watch.email_failed": "Review email could not be sent",
    "regulatory_watch.removed_sections_flagged": "Rule removed from the bulletin",
    "roi_snapshot.captured": "Savings snapshot captured",
    "port_rate.created": "Tariff added",
    "port_rate.updated": "Tariff corrected",
    "port_rate.superseded": "Tariff period closed",
    "spot_rate.created": "Spot rate added",
    "spot_rate.updated": "Spot rate corrected",
    "locked_rate.created": "Rate locked",
    "gazetted_rate.created": "Customs rate published",
    "gazetted_rate.revised": "Customs rate revised",
}
ENTITY_WORDS: dict[str, str] = {
    "agent_run": "Agent run",
    "shipment": "Shipment",
    "document": "Document",
    "compliance_rule": "Rule",
    "bulletin": "Bulletin",
    "bulletin_snapshot": "Bulletin scan",
    "bulletin_change": "Bulletin change",
    "port_rate": "Tariff",
    "spot_rate": "Spot rate",
    "locked_rate": "Locked rate",
    "gazetted_rate": "Customs rate",
    "roi_snapshot": "Savings snapshot",
}


def action_words(action: str) -> str:
    """An audit event's action in plain words."""
    return ACTION_WORDS.get(action, action.replace(".", ": ").replace("_", " ").capitalize())


def entity_word(entity_type: str) -> str:
    return ENTITY_WORDS.get(entity_type, entity_type.replace("_", " ").capitalize())


def agent_name(name: str) -> str:
    """The plain name of an agent (or the name itself for people and services)."""
    return AGENT_NAMES.get(name, name)


def issue_name(code: str) -> str:
    return ISSUE_NAMES.get(code, code.replace("_", " ").capitalize())


def status_word(status: Any) -> str:
    value = getattr(status, "value", status)
    return STATUS_WORDS.get(str(value), str(value).replace("_", " ").capitalize())


# --------------------------------------------------------------------------- time and numbers


def fmt_ist(value: datetime | None) -> str:
    """Render a stored UTC timestamp in IST, or a dash if unknown."""
    return value.astimezone(IST).strftime("%d %b %Y, %H:%M IST") if value else "-"


def fmt_day(value: date | None) -> str:
    """A calendar day the way people read it (e.g. 28 Sep 2026)."""
    return value.strftime("%d %b %Y") if value else "-"


def fmt_ist_day(value: datetime | None) -> str:
    """The IST calendar day of a stored UTC timestamp (for tables where the hour is noise)."""
    return fmt_day(value.astimezone(IST)) if value else "-"


def ist_to_utc(day: date, at: time) -> datetime:
    """Interpret a date + time picked in the UI as IST and return the aware UTC instant."""
    return datetime.combine(day, at, tzinfo=IST).astimezone(ZoneInfo("UTC"))


def split_ist(value: datetime) -> tuple[date, time]:
    """Split a stored UTC timestamp into IST date and time for pre-filling inputs."""
    local = value.astimezone(IST)
    return local.date(), local.time().replace(second=0, microsecond=0)


def to_decimal(value: float | None) -> Decimal | None:
    """Convert a Streamlit number_input float to Decimal via its string form (no binary noise)."""
    return None if value is None else Decimal(str(value))


# --------------------------------------------------------------------------- writes


def problem_text(exc: Exception) -> str:
    """A domain or validation error in plain words (no Python class names)."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            field = " ".join(str(p).replace("_", " ") for p in error["loc"]) or "input"
            if error["type"] == "string_pattern_mismatch":
                message = "is not in the expected format"
            else:
                message = error["msg"][:1].lower() + error["msg"][1:]
            parts.append(f"{field}: {message}")
        problems = "; ".join(parts)
        return f"Please check the form. {problems[:1].upper()}{problems[1:]}."
    message = str(exc).strip()
    return message[0].upper() + message[1:] if message else "That did not work."


def run_action(
    action: Callable[[Session], Any],
    success: str,
    *,
    then: Callable[[], None] | None = None,
) -> None:
    """Run one write in its own transaction; show problems inline, or flash + rerun.

    ``then`` runs after a successful commit, before the rerun (e.g. to queue which record the
    next run should open)."""
    try:
        with session_scope() as session:
            action(session)
    except (ValidationError, CustomsCopilotError) as exc:
        st.error(problem_text(exc))
    else:
        flash(success)
        if then is not None:
            then()
        st.rerun()


def flash(message: str) -> None:
    """Queue a success message for the next run (shown by :func:`show_flash`)."""
    st.session_state[_FLASH_KEY] = message


def show_flash() -> None:
    """Show (once) the success message left by the previous run's action, as a toast in the
    corner: the page keeps its place (tab, scroll) and nothing above the action moves."""
    message = st.session_state.pop(_FLASH_KEY, None)
    if message:
        st.toast(message, icon=":material/check_circle:", duration="long")

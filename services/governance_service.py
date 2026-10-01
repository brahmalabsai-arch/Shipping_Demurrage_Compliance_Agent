"""Governance of the supervised clearance runs (Phase 11): who may decide, which run waits at
which human gate, and the filing decision.

* **Reviewers** — the human decisions (compliance approvals/revocations on the dashboard, the
  filing gate) are taken by people named in the ``REVIEWERS`` setting; :func:`require_reviewer`
  refuses anyone else with ``UnknownReviewerError``.
* **Case runs** (``governance.case_runs``) — an index of the supervisor's runs, one row per
  ``run_id``: its state (``RunState``) and, at the compliance gate, the pending rule versions it
  waits on. At most one open run per shipment (partial unique index); a terminal state is final.
  The run's full state is its LangGraph checkpoint; its history is ``audit.events``.
* **Filing decisions** (``governance.filing_decisions``) — a reviewer's final approve/reject at a
  run's filing gate, append-only, one per run, stored with the checklist the reviewer saw. An
  approval of a checklist that is not ready is impossible (``IncompleteCaseError``, and a CHECK
  constraint); ``shipping_service.clear_shipment`` needs an approval from here.

Functions take the caller's ``Session`` and only flush; every change is audited in the same
transaction (conventions §3).
"""

import re
from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import get_settings
from models.db_models import CaseRun, FilingDecision, Shipment
from models.enums import OPEN_RUN_STATES, RuleDecision, RunState, ShipmentStatus
from models.schemas import CaseRunRecord, FilingDecisionRecord, Reviewer
from services import audit_service
from services.errors import (
    CaseRunNotFoundError,
    FilingAlreadyDecidedError,
    FilingDecisionNotFoundError,
    IncompleteCaseError,
    InvalidFilingDecisionError,
    RunAlreadyFinishedError,
    RunAlreadyOpenError,
    RunNotAtGateError,
    ShipmentCancelledError,
    ShipmentNotFoundError,
    UnknownReviewerError,
)

# --------------------------------------------------------------------------- reviewers

_ENTRY = re.compile(r"^(?P<name>[^<>]+?)\s*(?:<(?P<email>[^<>\s]+)>)?$")


def reviewers() -> list[Reviewer]:
    """The configured reviewer registry (``REVIEWERS``: "Name <email>; Other Name"), in order."""
    raw = get_settings().reviewers or ""
    found: list[Reviewer] = []
    for entry in re.split(r"[;,]", raw):
        match = _ENTRY.match(entry.strip())
        if match and match["name"].strip():
            found.append(Reviewer(name=match["name"].strip(), email=match["email"]))
    return found


def require_reviewer(name: str) -> Reviewer:
    """The registered reviewer called ``name`` (case-insensitive), else ``UnknownReviewerError``."""
    wanted = name.strip().casefold()
    for reviewer in reviewers():
        if reviewer.name.casefold() == wanted:
            return reviewer
    known = ", ".join(r.name for r in reviewers()) or "none configured (set REVIEWERS)"
    raise UnknownReviewerError(f"{name.strip() or '(blank)'} is not a registered reviewer; {known}")


# --------------------------------------------------------------------------- case runs


def _load_run(session: Session, run_id: str, *, lock: bool = False) -> CaseRun:
    row = session.get(CaseRun, run_id, with_for_update=lock)
    if row is None:
        raise CaseRunNotFoundError(f"no clearance run {run_id}")
    return row


def get_run(session: Session, run_id: str) -> CaseRunRecord:
    """One clearance run by its ``run_id``."""
    return CaseRunRecord.model_validate(_load_run(session, run_id))


def list_runs(
    session: Session, shipment_id: UUID | None = None, *, open_only: bool = False
) -> list[CaseRunRecord]:
    """Clearance runs, newest first — optionally of one shipment, optionally only open ones."""
    query = select(CaseRun).order_by(CaseRun.started_at.desc(), CaseRun.run_id)
    if shipment_id is not None:
        query = query.where(CaseRun.shipment_id == shipment_id)
    if open_only:
        query = query.where(CaseRun.state.in_(OPEN_RUN_STATES))
    return [CaseRunRecord.model_validate(row) for row in session.scalars(query)]


def runs_awaiting_rule(session: Session, rule_id: UUID) -> list[CaseRunRecord]:
    """Runs whose compliance gate waits on this pending rule version."""
    query = select(CaseRun).where(
        CaseRun.state == RunState.AWAITING_COMPLIANCE, CaseRun.awaiting_rule_ids.contains([rule_id])
    )
    return [CaseRunRecord.model_validate(row) for row in session.scalars(query)]


def start_run(
    session: Session, run_id: str, shipment_id: UUID, as_of: date, actor: str
) -> CaseRunRecord:
    """Register a new supervised run (state ``running``). A cancelled shipment cannot start one
    (``ShipmentCancelledError``); a shipment with an open run raises ``RunAlreadyOpenError``."""
    shipment = session.get(Shipment, shipment_id)
    if shipment is None:
        raise ShipmentNotFoundError(f"no shipment with id {shipment_id}")
    if shipment.status is ShipmentStatus.CANCELLED:
        raise ShipmentCancelledError(f"shipment {shipment.container_id} is cancelled")
    row = CaseRun(
        run_id=run_id,
        shipment_id=shipment_id,
        as_of=as_of,
        state=RunState.RUNNING,
        awaiting_rule_ids=[],
        started_by=actor,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "uq_case_runs_open_shipment" in str(exc.orig):
            raise RunAlreadyOpenError(
                f"{shipment.container_id} already has an open clearance run"
            ) from exc
        if "pk_case_runs" in str(exc.orig):
            raise RunAlreadyOpenError(f"clearance run {run_id} already exists") from exc
        raise
    session.refresh(row)
    record = CaseRunRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="case_run.started",
        entity_type="shipment",
        entity_id=shipment_id,
        payload={"run_id": run_id, "as_of": as_of},
    )
    session.flush()
    return record


def update_run(
    session: Session,
    run_id: str,
    state: RunState,
    *,
    actor: str,
    awaiting_rule_ids: Sequence[UUID] = (),
    detail: str | None = None,
) -> CaseRunRecord:
    """Move a run to ``state`` (the supervisor, after each step of the graph). A finished run
    cannot move again (``RunAlreadyFinishedError``); an identical update writes nothing."""
    row = _load_run(session, run_id, lock=True)
    if row.state not in OPEN_RUN_STATES:
        raise RunAlreadyFinishedError(f"clearance run {run_id} already ended as {row.state}")
    waiting = list(awaiting_rule_ids) if state is RunState.AWAITING_COMPLIANCE else []
    if (row.state, list(row.awaiting_rule_ids), row.detail) == (state, waiting, detail):
        return CaseRunRecord.model_validate(row)
    previous = row.state
    row.state = state
    row.awaiting_rule_ids = waiting
    row.detail = detail
    session.flush()
    session.refresh(row)
    record = CaseRunRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="case_run.state_changed",
        entity_type="shipment",
        entity_id=row.shipment_id,
        payload={
            "run_id": run_id,
            "from": previous,
            "to": state,
            "awaiting_rule_ids": waiting,
            "detail": detail,
        },
    )
    session.flush()
    return record


def abandon_run(session: Session, run_id: str, reviewer: str, reason: str) -> CaseRunRecord:
    """End an open run by hand (e.g. a run left ``running`` by a crashed process) as ``failed``."""
    reason = reason.strip()
    if not reviewer.strip() or not reason:
        raise InvalidFilingDecisionError("abandoning a run needs a reviewer and a reason")
    return update_run(
        session,
        run_id,
        RunState.FAILED,
        actor=reviewer.strip(),
        detail=f"abandoned by {reviewer.strip()}: {reason}",
    )


# --------------------------------------------------------------------------- filing decisions


def record_filing_decision(
    session: Session,
    run_id: str,
    decision: RuleDecision | str,
    reviewer: str,
    note: str | None,
    *,
    checklist: dict[str, Any],
    ready_for_clearance: bool,
) -> FilingDecisionRecord:
    """Record the reviewer's final decision at the run's filing gate.

    The run must be ``awaiting_filing`` (``RunNotAtGateError`` / ``RunAlreadyFinishedError``);
    one decision per run (``FilingAlreadyDecidedError``). A blank reviewer, an unknown decision
    or a rejection without a reason raise ``InvalidFilingDecisionError``; approving a checklist
    that is not ready raises ``IncompleteCaseError``. Audited ``filing.approved`` /
    ``filing.rejected`` under the reviewer's name.
    """
    reviewer = reviewer.strip()
    note = (note or "").strip() or None
    try:
        verdict = RuleDecision(decision)
    except ValueError as exc:
        raise InvalidFilingDecisionError(
            f"decision must be 'approve' or 'reject', not {decision!r}"
        ) from exc
    if not reviewer:
        raise InvalidFilingDecisionError("a filing decision must name the human reviewer")
    if verdict is RuleDecision.REJECT and note is None:
        raise InvalidFilingDecisionError("a filing rejection must give a reason")

    run = _load_run(session, run_id, lock=True)
    if session.scalars(select(FilingDecision.id).where(FilingDecision.run_id == run_id)).first():
        raise FilingAlreadyDecidedError(f"clearance run {run_id} already has its filing decision")
    if run.state not in OPEN_RUN_STATES:
        raise RunAlreadyFinishedError(f"clearance run {run_id} already ended as {run.state}")
    if run.state is not RunState.AWAITING_FILING:
        raise RunNotAtGateError(f"clearance run {run_id} is {run.state}, not at the filing gate")
    if verdict is RuleDecision.APPROVE and not ready_for_clearance:
        raise IncompleteCaseError(
            f"clearance run {run_id}: the checklist is not ready for clearance — resolve its "
            "blockers, data gaps and conflicts and analyse again"
        )

    row = FilingDecision(
        run_id=run_id,
        shipment_id=run.shipment_id,
        decision=verdict,
        reviewer=reviewer,
        note=note,
        ready_for_clearance=ready_for_clearance,
        checklist=checklist,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "uq_filing_decisions_run_id" in str(exc.orig):
            raise FilingAlreadyDecidedError(
                f"clearance run {run_id} already has its filing decision"
            ) from exc
        raise
    session.refresh(row)
    record = FilingDecisionRecord.model_validate(row)
    audit_service.record(
        session,
        actor=reviewer,
        action=f"filing.{'approved' if verdict is RuleDecision.APPROVE else 'rejected'}",
        entity_type="shipment",
        entity_id=run.shipment_id,
        payload={"run_id": run_id, "filing_decision_id": row.id, "note": note},
    )
    session.flush()
    return record


def get_filing_decision(session: Session, decision_id: UUID) -> FilingDecisionRecord:
    """One filing decision by id."""
    row = session.get(FilingDecision, decision_id)
    if row is None:
        raise FilingDecisionNotFoundError(f"no filing decision with id {decision_id}")
    return FilingDecisionRecord.model_validate(row)


def filing_decision_for_run(session: Session, run_id: str) -> FilingDecisionRecord | None:
    """The run's filing decision, if one was taken."""
    row = session.scalars(select(FilingDecision).where(FilingDecision.run_id == run_id)).first()
    return None if row is None else FilingDecisionRecord.model_validate(row)

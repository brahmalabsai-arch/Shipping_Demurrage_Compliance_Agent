"""Phase 11 — governance_service: the reviewer registry, clearance runs and filing decisions."""

from datetime import date
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import Settings
from models.db_models import AuditEvent
from models.enums import RuleDecision, RunState, ShipmentStatus
from models.schemas import Reviewer
from services import governance_service as svc
from services import shipping_service
from services.errors import (
    CaseRunNotFoundError,
    FilingAlreadyDecidedError,
    IncompleteCaseError,
    InvalidFilingDecisionError,
    RunAlreadyFinishedError,
    RunAlreadyOpenError,
    RunNotAtGateError,
    ShipmentCancelledError,
    ShipmentNotFoundError,
    UnknownReviewerError,
)
from tests.fixtures.shipments import make_shipment

AS_OF = date(2026, 9, 28)
REVIEWER = "Asha Rao"


def _shipment(session: Session, **overrides: object) -> UUID:
    return shipping_service.create_shipment(session, make_shipment(**overrides), "test").id


_containers = iter(range(1000000, 9999999))


def _run(session: Session, state: RunState = RunState.AWAITING_FILING) -> str:
    run_id = str(uuid4())
    svc.start_run(session, run_id, _shipment(session, container_id=f"TSTU{next(_containers)}"),
                  AS_OF, "ops")  # fmt: skip
    if state is not RunState.RUNNING:
        svc.update_run(session, run_id, state, actor="supervisor")
    return run_id


# --------------------------------------------------------------------------- reviewers


def _registry(monkeypatch: pytest.MonkeyPatch, reviewers: str | None) -> None:
    """Own Settings (no .env needed — CI's unit job has none), seen by governance_service."""
    settings = Settings(_env_file=None, postgres_password=SecretStr("unused"), reviewers=reviewers)
    monkeypatch.setattr(svc, "get_settings", lambda: settings)


def test_reviewers_come_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, " Rupin <4x2kms@gmail.com>; Asha Rao ,, ")

    assert svc.reviewers() == [Reviewer(name="Rupin", email="4x2kms@gmail.com"),
                               Reviewer(name="Asha Rao")]  # fmt: skip
    assert svc.require_reviewer(" rupin ").name == "Rupin"  # case-insensitive, trimmed
    with pytest.raises(UnknownReviewerError, match="Rupin, Asha Rao"):
        svc.require_reviewer("Mallory")


def test_no_registry_means_nobody_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, None)
    assert svc.reviewers() == []
    with pytest.raises(UnknownReviewerError, match="set REVIEWERS"):
        svc.require_reviewer(REVIEWER)


# --------------------------------------------------------------------------- runs


def test_a_run_starts_running_and_is_audited(db_session: Session) -> None:
    sid = _shipment(db_session)
    run = svc.start_run(db_session, "11111111-1111-1111-1111-111111111111", sid, AS_OF, "ops")

    assert (run.state, run.awaiting_rule_ids, run.started_by) == (RunState.RUNNING, [], "ops")
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "case_run.started")
    ).one()
    assert event.entity_id == sid and event.payload["run_id"] == run.run_id


def test_one_open_run_per_shipment(db_session: Session) -> None:
    sid = _shipment(db_session)
    first = svc.start_run(db_session, str(uuid4()), sid, AS_OF, "ops")
    with pytest.raises(RunAlreadyOpenError):
        svc.start_run(db_session, str(uuid4()), sid, AS_OF, "ops")
    with pytest.raises(RunAlreadyOpenError):  # and run ids are unique
        svc.start_run(db_session, first.run_id, _shipment(db_session, container_id="TSTU0000002"),
                      AS_OF, "ops")  # fmt: skip

    svc.update_run(db_session, first.run_id, RunState.BLOCKED, actor="supervisor", detail="x")
    assert svc.start_run(db_session, str(uuid4()), sid, AS_OF, "ops").state is RunState.RUNNING
    assert [r.state for r in svc.list_runs(db_session, sid)] == [RunState.RUNNING,
                                                                  RunState.BLOCKED]  # fmt: skip
    assert len(svc.list_runs(db_session, sid, open_only=True)) == 1


def test_a_run_needs_a_live_shipment(db_session: Session) -> None:
    with pytest.raises(ShipmentNotFoundError):
        svc.start_run(db_session, str(uuid4()), uuid4(), AS_OF, "ops")
    sid = _shipment(db_session, status=ShipmentStatus.BOOKED)
    shipping_service.update_status(db_session, sid, ShipmentStatus.CANCELLED, "ops")
    with pytest.raises(ShipmentCancelledError):
        svc.start_run(db_session, str(uuid4()), sid, AS_OF, "ops")


def test_terminal_states_are_final(db_session: Session) -> None:
    run_id = _run(db_session, RunState.RUNNING)
    rule_id = uuid4()
    waiting = svc.update_run(db_session, run_id, RunState.AWAITING_COMPLIANCE, actor="supervisor",
                             awaiting_rule_ids=[rule_id])  # fmt: skip
    assert waiting.awaiting_rule_ids == [rule_id]
    assert [r.run_id for r in svc.runs_awaiting_rule(db_session, rule_id)] == [run_id]

    svc.update_run(db_session, run_id, RunState.FAILED, actor="supervisor", detail="boom")
    with pytest.raises(RunAlreadyFinishedError):
        svc.update_run(db_session, run_id, RunState.RUNNING, actor="supervisor")
    assert svc.runs_awaiting_rule(db_session, rule_id) == []
    with pytest.raises(CaseRunNotFoundError):
        svc.get_run(db_session, "no-such-run")


def test_abandoning_an_open_run(db_session: Session) -> None:
    run_id = _run(db_session, RunState.RUNNING)
    with pytest.raises(InvalidFilingDecisionError):
        svc.abandon_run(db_session, run_id, REVIEWER, " ")
    run = svc.abandon_run(db_session, run_id, REVIEWER, "process crashed")
    assert (run.state, run.detail) == (RunState.FAILED, f"abandoned by {REVIEWER}: process crashed")


# --------------------------------------------------------------------------- filing decisions


def test_an_approval_is_recorded_with_the_checklist_seen(db_session: Session) -> None:
    run_id = _run(db_session)
    decision = svc.record_filing_decision(db_session, run_id, "approve", f" {REVIEWER} ", None,
                                          checklist={"summary": "ready"}, ready_for_clearance=True)  # fmt: skip

    assert (decision.decision, decision.reviewer, decision.checklist) == (
        RuleDecision.APPROVE, REVIEWER, {"summary": "ready"}
    )  # fmt: skip
    assert svc.filing_decision_for_run(db_session, run_id) == decision
    assert svc.get_filing_decision(db_session, decision.id) == decision
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "filing.approved")
    ).one()
    assert event.actor == REVIEWER and event.payload["filing_decision_id"] == str(decision.id)


@pytest.mark.parametrize(
    ("decision", "reviewer", "note", "ready", "error"),
    [
        ("approve", REVIEWER, None, False, IncompleteCaseError),
        ("reject", REVIEWER, " ", True, InvalidFilingDecisionError),
        ("approve", " ", None, True, InvalidFilingDecisionError),
        ("maybe", REVIEWER, None, True, InvalidFilingDecisionError),
    ],
)
def test_invalid_filing_decisions_record_nothing(
    db_session: Session, decision: str, reviewer: str, note: str | None, ready: bool,
    error: type[Exception],
) -> None:  # fmt: skip
    run_id = _run(db_session)
    with pytest.raises(error):
        svc.record_filing_decision(db_session, run_id, decision, reviewer, note, checklist={},
                                   ready_for_clearance=ready)  # fmt: skip
    assert svc.filing_decision_for_run(db_session, run_id) is None


def test_a_filing_decision_only_at_the_filing_gate_and_only_once(db_session: Session) -> None:
    waiting = _run(db_session, RunState.AWAITING_COMPLIANCE)
    ended = _run(db_session, RunState.BLOCKED)
    for run_id, error in ((waiting, RunNotAtGateError), (ended, RunAlreadyFinishedError)):
        with pytest.raises(error):
            svc.record_filing_decision(db_session, run_id, "approve", REVIEWER, None,
                                       checklist={}, ready_for_clearance=True)  # fmt: skip

    run_id = _run(db_session)
    svc.record_filing_decision(db_session, run_id, "reject", REVIEWER, "query", checklist={},
                               ready_for_clearance=True)  # fmt: skip
    with pytest.raises(FilingAlreadyDecidedError):
        svc.record_filing_decision(db_session, run_id, "approve", REVIEWER, None, checklist={},
                                   ready_for_clearance=True)  # fmt: skip


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ("'approve', 'x', NULL, false", "approval_needs_ready_case"),
        ("'reject', 'x', ' ', true", "rejection_has_reason"),
        ("'approve', ' ', NULL, true", "names_reviewer"),
    ],
)
def test_database_checks_filing_decisions(db_session: Session, values: str, message: str) -> None:
    run_id = _run(db_session)
    sid = svc.get_run(db_session, run_id).shipment_id
    with pytest.raises(IntegrityError, match=message), db_session.begin_nested():
        db_session.execute(
            text("INSERT INTO governance.filing_decisions (run_id, shipment_id, decision, reviewer,"
                 f" note, ready_for_clearance, checklist) VALUES (:r, :s, {values}, '{{}}')"),
            {"r": run_id, "s": sid},
        )  # fmt: skip


def test_filing_decisions_are_append_only(db_session: Session) -> None:
    run_id = _run(db_session)
    svc.record_filing_decision(db_session, run_id, "reject", REVIEWER, "query", checklist={},
                               ready_for_clearance=True)  # fmt: skip
    for statement in ("UPDATE governance.filing_decisions SET note = 'edited'",
                      "DELETE FROM governance.filing_decisions"):  # fmt: skip
        with pytest.raises(IntegrityError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(statement))

"""Phase 11 — the supervisor: the pipeline behind the compliance-approval and filing gates,
checkpointed in Postgres (exit criteria and ``edge_cases.md`` Phase 11).

Runs use in-memory MCP servers against the rolled-back test transaction; the checkpoints go to
the test database's ``checkpoints`` schema (``checkpoints`` fixture). The separate-process exit
criterion needs committed data instead (``committed`` fixture: real commits, tables emptied
afterwards).
"""

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from chromadb.api.shared_system_client import SharedSystemClient
from langgraph.types import Command
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agents import checkpoint, compliance_rag_agent, supervisor
from agents.mcp_clients import memory_connector
from agents.state import CaseState, Checklist, ChecklistItem, Conflict
from config.settings import PROJECT_ROOT, get_settings
from models.db_models import DOMAIN_SCHEMAS, AuditEvent
from models.enums import RuleDecision, RuleStatus, RunState, ShipmentStatus
from models.schemas import DocumentAmend, FilingDecisionRecord, RuleProposal
from rag import store
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import compliance_service, governance_service, shipping_service
from services import db as db_module
from services.errors import (
    ClearanceRequiresApprovalError,
    DecisionPendingError,
    FilingAlreadyDecidedError,
    IncompleteCaseError,
    InvalidFilingDecisionError,
    RuleAlreadyDecidedError,
    RunAlreadyFinishedError,
    RunAlreadyOpenError,
    RunNotAtGateError,
    UnknownReviewerError,
)
from tests.fixtures.shipments import make_shipment

AS_OF = date(2026, 9, 28)
REVIEWER, OTHER_REVIEWER = "Asha Rao", "Vikram Shah"  # registered in tests/conftest.py
CLEAN, QTY_MISMATCH, HELD = "MSKU7001001", "MSCU7002002", "HLXU7004004"
DUTY = "HSN-8708-duty"
DUTY_V2 = (
    "Parts and accessories of motor vehicles (heading 8708) attract basic customs duty (BCD) of\n"
    "25% of the assessable value, a Social Welfare Surcharge of 10% of the BCD, and IGST of 28%\n"
    "on the assessable value plus BCD and surcharge."
)


# --------------------------------------------------------------------------- fixtures / helpers


def seed_all() -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()


@pytest.fixture
def seeded(session_factory: sessionmaker[Session], checkpoints: None) -> None:
    seed_all()


@pytest.fixture
def committed(
    db_engine: Engine, checkpoints: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Real commits on the test database (another process must see them); every domain table
    is emptied afterwards (TRUNCATE bypasses the append-only row triggers)."""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    yield
    with db_engine.begin() as conn:
        tables = conn.scalars(
            text("SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname = ANY(:s)"),
            {"s": list(DOMAIN_SCHEMAS)},
        ).all()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


def shipment_id(container_id: str) -> UUID:
    with db_module.session_scope() as session:
        return shipping_service.get_shipment_by_container_id(session, container_id).id


def status_of(sid: UUID) -> ShipmentStatus:
    with db_module.session_scope() as session:
        return shipping_service.get_shipment(session, sid).status


def propose(rule_key: str, content: str, hs_codes: list[str]) -> UUID:
    with db_module.session_scope() as session:
        proposal = RuleProposal(rule_key=rule_key, content=content, hs_codes=hs_codes,
                                effective_date=date(2026, 10, 1))  # fmt: skip
        return compliance_service.propose_change(session, proposal, "test").id


def decide(rule_id: UUID, decision: str, note: str | None = None) -> None:
    with db_module.session_scope() as session:
        compliance_service.decide(session, rule_id, decision, REVIEWER, note)


def events(action: str, run_id: str | None = None) -> list[AuditEvent]:
    with db_module.session_scope() as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.created_at)
        ).all()
    return [e for e in rows if run_id is None or e.payload.get("run_id") == run_id]


def case_run(run_id: str) -> Any:
    with db_module.session_scope() as session:
        return governance_service.get_run(session, run_id)


async def start(container_id: str) -> supervisor.ClearanceRun:
    return await supervisor.start_clearance_run(
        shipment_id(container_id), connector=memory_connector, model_factory=lambda: None,
        as_of=AS_OF,
    )  # fmt: skip


async def resume_compliance(run_id: str, rule_id: UUID) -> supervisor.ClearanceRun:
    return await supervisor.resume_after_compliance_decision(
        run_id, rule_id, connector=memory_connector, model_factory=lambda: None
    )


async def resume_filing(
    run_id: str, decision: str, reviewer: str = REVIEWER, note: str | None = None
) -> supervisor.ClearanceRun:
    return await supervisor.resume_after_filing_approval(
        run_id, decision, reviewer, note, connector=memory_connector
    )


async def poke(run_id: str, payload: Any) -> list[dict[str, Any]]:
    """Drive the checkpointed graph directly (not through a resumption function) — what the
    gates must withstand. Returns the requests still open afterwards."""
    async with AsyncExitStack() as stack:
        deps = await supervisor._open_deps(stack, run_id, (), memory_connector, None)
        with checkpoint.open_checkpointer() as saver:
            graph = supervisor.build_supervisor(saver)
            config: Any = {"configurable": {"thread_id": run_id, **deps}}
            await graph.ainvoke(payload, config)
            return supervisor._open_requests(await graph.aget_state(config))


def duty_finding(case: CaseState) -> Any:
    return next(f for f in case.compliance_findings if f.question.startswith("What basic customs"))


# --------------------------------------------------------------------------- exit criterion 1


async def test_compliance_gate_resumes_after_a_decision_taken_in_another_process(
    committed: None, tmp_path: Path
) -> None:
    """A run interrupted at the compliance gate resumes from its checkpoint after a separate
    process (standing in for the dashboard) calls ``compliance_service.decide``."""
    seed_all()
    rule_id = propose(DUTY, DUTY_V2, ["8708"])
    run = await start(CLEAN)
    run_id = run.run.run_id

    assert run.run.state is RunState.AWAITING_COMPLIANCE
    assert run.run.awaiting_rule_ids == [rule_id]
    assert [(r["gate"], r["rule_key"], r["version"]) for r in run.waiting_on] == [
        ("compliance", DUTY, 2)
    ]
    assert [c.version for c in duty_finding(run.case).citations] == [1]
    with pytest.raises(DecisionPendingError):  # nothing to resume on yet
        await resume_compliance(run_id, rule_id)

    # --- the decision, in another process (its own DB connection, its own Python)
    code = (
        "import sys; from uuid import UUID\n"
        "from rag import models; from tests.fixtures.rag import FakeEmbedder\n"
        "embedder = FakeEmbedder(); models.get_embedder = lambda: embedder\n"
        "from services import compliance_service; from services.db import session_scope\n"
        "with session_scope() as s:\n"
        "    compliance_service.decide(s, UUID(sys.argv[1]), 'approve', 'Asha Rao', 'notified')\n"
    )
    env = {**os.environ, "POSTGRES_DB": get_settings().postgres_test_db,
           "CHROMA_PATH": str(get_settings().chroma_path), "PYTHONIOENCODING": "utf-8"}  # fmt: skip
    done = subprocess.run([sys.executable, "-c", code, str(rule_id)], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert case_run(run_id).state is RunState.AWAITING_COMPLIANCE  # a decision alone moves no run

    # This process reopens the index the other process wrote (in production the rulebook MCP
    # server is a fresh process per run anyway).
    store._clients.clear()
    SharedSystemClient.clear_system_cache()
    resumed = await resume_compliance(run_id, rule_id)

    case = resumed.case
    assert [(d.rule_key, d.version, d.outcome, d.reviewer) for d in case.compliance_decisions] == [
        (DUTY, 2, RuleStatus.APPROVED, REVIEWER)
    ]
    assert [c.version for c in duty_finding(case).citations] == [2]  # the rulebook was re-asked
    assert len(events("compliance_rag.completed", run_id)) == 2
    assert len(events("clearance_checklist.completed", run_id)) == 2
    assert [e.payload["outcome"] for e in events("supervisor.compliance_approved", run_id)] == [
        "approved"
    ]
    assert resumed.run.state is RunState.AWAITING_FILING  # continued to the next gate
    assert [r["gate"] for r in resumed.waiting_on] == ["filing"]
    assert case.run_id == run_id and case.checklist is not None


# --------------------------------------------------------------------------- exit criterion 2


async def test_the_filing_gate_opens_only_to_an_explicit_approval(seeded: None) -> None:
    sid = shipment_id(CLEAN)
    run = await start(CLEAN)
    run_id = run.run.run_id
    assert run.run.state is RunState.AWAITING_FILING
    assert run.waiting_on[0]["gate"] == "filing"

    # driving the graph directly: re-running it, generic "continue"/"approve" values, a made-up
    # decision id, a compliance-shaped answer — the gate refuses them all and keeps waiting
    payloads: list[Any] = [
        None,
        Command(resume="approve"),
        Command(resume={"gate": "filing", "run_id": run_id, "approved": True}),
        Command(resume={"gate": "filing", "run_id": run_id, "filing_decision_id": str(uuid4())}),
        Command(resume={"gate": "compliance", "rule_id": str(uuid4())}),
    ]
    for payload in payloads:
        still_open = await poke(run_id, payload)
        assert [r["gate"] for r in still_open] == ["filing"], payload
    # the other resumption function is not a way through
    with pytest.raises(RunNotAtGateError):
        await resume_compliance(run_id, uuid4())
    # nor a plain status change, through the service or straight in SQL
    with pytest.raises(ClearanceRequiresApprovalError), db_module.session_scope() as session:
        shipping_service.update_status(session, sid, ShipmentStatus.UNDER_REVIEW, "ops")
        shipping_service.update_status(session, sid, ShipmentStatus.CLEARED, "ops")
    # (a short-lived session: never hold one open across an await — see tests/conftest.py)
    with (
        pytest.raises(IntegrityError, match="approved filing decision"),
        db_module.session_scope() as session,
    ):
        session.execute(
            text("UPDATE shipping.shipments SET status = 'cleared' WHERE id = :id"), {"id": sid}
        )
    # nor an unregistered person
    with pytest.raises(UnknownReviewerError):
        await resume_filing(run_id, "approve", reviewer="Somebody Else")

    assert status_of(sid) is ShipmentStatus.DOCKED
    assert case_run(run_id).state is RunState.AWAITING_FILING
    assert events("filing.approved") == []

    done = await resume_filing(run_id, "approve")  # the explicit approval

    assert done.run.state is RunState.FILED
    assert status_of(sid) is ShipmentStatus.CLEARED
    assert done.case.outcome == "filed" and done.case.filing_decision is not None
    [approved] = events("filing.approved", run_id)
    assert approved.actor == REVIEWER
    moves = [(e.payload["from"], e.payload["to"], e.actor)
             for e in events("shipment.status_changed") if e.entity_id == sid]  # fmt: skip
    assert moves[-2:] == [("docked", "under_review", "supervisor"), ("under_review", "cleared",
                                                                "supervisor")]  # fmt: skip


# --------------------------------------------------------------------------- exit criterion 3


async def test_a_filing_rejection_is_terminal_and_logged(seeded: None) -> None:
    sid = shipment_id(CLEAN)
    run_id = (await start(CLEAN)).run.run_id

    with pytest.raises(InvalidFilingDecisionError):  # a rejection must say why
        await resume_filing(run_id, "reject")
    rejected = await resume_filing(run_id, "reject", note="valuation query from the CHA")

    assert rejected.run.state is RunState.FILING_REJECTED
    assert rejected.case.outcome == "filing_rejected"
    assert (
        rejected.case.outcome_detail
        == f"filing rejected by {REVIEWER}: valuation query from the CHA"
    )
    assert status_of(sid) is ShipmentStatus.DOCKED
    [event] = events("filing.rejected", run_id)
    assert event.actor == REVIEWER and event.payload["note"] == "valuation query from the CHA"
    assert [e.payload["to"] for e in events("case_run.state_changed", run_id)][
        -1
    ] == "filing_rejected"

    # never retried as approved: not by the resumption function, not by the service, not in SQL
    with pytest.raises(RunAlreadyFinishedError):
        await resume_filing(run_id, "approve")
    with pytest.raises(FilingAlreadyDecidedError), db_module.session_scope() as session:
        governance_service.record_filing_decision(session, run_id, "approve", OTHER_REVIEWER,
                                                  None, checklist={}, ready_for_clearance=True)  # fmt: skip
    with pytest.raises(IntegrityError, match="immutable"), db_module.session_scope() as session:
        session.execute(text("UPDATE governance.filing_decisions SET decision = 'approve'"
                             " WHERE run_id = :r"), {"r": run_id})  # fmt: skip
    assert await poke(run_id, Command(resume="approve")) == []  # the run has ended
    assert status_of(sid) is ShipmentStatus.DOCKED
    # a new run is how the shipment is tried again
    retry = await start(CLEAN)
    assert retry.run.run_id != run_id and retry.run.state is RunState.AWAITING_FILING


async def test_a_compliance_rejection_is_final_and_the_run_continues_on_the_rulebook_in_force(
    seeded: None,
) -> None:
    rule_id = propose(DUTY, DUTY_V2, ["8708"])
    run_id = (await start(CLEAN)).run.run_id
    decide(rule_id, "reject", "not notified by CBIC")

    resumed = await resume_compliance(run_id, rule_id)

    [decision] = resumed.case.compliance_decisions
    assert (decision.outcome, decision.note) == (RuleStatus.REJECTED, "not notified by CBIC")
    [event] = events("supervisor.compliance_rejected", run_id)
    assert event.payload["rule_key"] == DUTY and event.actor == "supervisor"
    assert len(events("compliance_rag.completed", run_id)) == 1  # nothing changed: not re-asked
    assert [c.version for c in duty_finding(resumed.case).citations] == [1]
    assert resumed.run.state is RunState.AWAITING_FILING
    with pytest.raises(RuleAlreadyDecidedError):  # the rejection is final
        decide(rule_id, "approve")
    with pytest.raises(RunNotAtGateError):  # and the run no longer waits on that rule
        await resume_compliance(run_id, rule_id)


# --------------------------------------------------------------------------- edge cases


async def test_revoking_an_approval_is_a_new_event_not_an_edit(seeded: None) -> None:
    rule_id = propose(DUTY, DUTY_V2, ["8708"])
    decide(rule_id, "approve", "notified")
    with db_module.session_scope() as session:
        approved = compliance_service.get_rule(session, rule_id)
        compliance_service.revoke_decision(
            session, rule_id, OTHER_REVIEWER, "notification withdrawn"
        )
    with db_module.session_scope() as session:
        revoked = compliance_service.get_rule(session, rule_id)
        in_force = compliance_service.get_current_rule(session, DUTY)

    assert revoked.status is RuleStatus.REVOKED
    assert (revoked.decided_by, revoked.decided_at, revoked.decision_note) == (
        approved.decided_by, approved.decided_at, approved.decision_note
    )  # fmt: skip
    assert (revoked.revoked_by, revoked.revoke_note) == (OTHER_REVIEWER, "notification withdrawn")
    [approval] = [e for e in events("compliance_rule.approved") if e.entity_id == rule_id]
    [revocation] = [e for e in events("compliance_rule.revoked") if e.entity_id == rule_id]
    assert revocation.id != approval.id and revocation.actor == OTHER_REVIEWER
    assert approval.payload["decision"] == "approve"  # the approval event is untouched
    assert in_force.version == 1  # the version it had superseded is back in force
    [reinstated] = events("compliance_rule.reinstated")
    assert reinstated.entity_id == in_force.id


async def test_two_decisions_on_two_pending_gates_of_one_run_are_serialised(seeded: None) -> None:
    duty_id = propose(DUTY, DUTY_V2, ["8708"])
    marking_id = propose("HSN-8708-marking", "Brake parts must carry the maker's mark.", ["870830"])
    run = await start(CLEAN)
    run_id = run.run.run_id
    assert sorted(run.run.awaiting_rule_ids) == sorted([duty_id, marking_id])
    assert sorted(r["rule_id"] for r in run.waiting_on) == sorted(map(str, (duty_id, marking_id)))
    decide(duty_id, "approve", "notified")
    decide(marking_id, "reject", "draft text")

    results = await asyncio.gather(
        resume_compliance(run_id, duty_id), resume_compliance(run_id, marking_id)
    )
    # gather returns in call order; the lock decides which resumption ran first
    first, second = sorted(results, key=lambda r: r.run.state is RunState.AWAITING_FILING)

    outcomes = {d.rule_id: d.outcome for d in second.case.compliance_decisions}
    assert outcomes == {duty_id: RuleStatus.APPROVED, marking_id: RuleStatus.REJECTED}
    assert len(second.case.compliance_decisions) == 2  # each applied once, neither lost
    # whichever ran first saw the other gate still open; the second one moved the run on
    assert first.run.state is RunState.AWAITING_COMPLIANCE
    assert [r["gate"] for r in first.waiting_on] == ["compliance"]
    assert second.run.state is RunState.AWAITING_FILING
    assert len(events("supervisor.resumed", run_id)) == 2
    assert len(events("compliance_rag.completed", run_id)) == 2  # one re-ask, after the approval
    final = await supervisor.inspect_run(run_id)
    assert final.case.compliance_decisions == second.case.compliance_decisions


async def test_a_resume_before_the_decision_changes_nothing(seeded: None) -> None:
    duty_id = propose(DUTY, DUTY_V2, ["8708"])
    marking_id = propose("HSN-8708-marking", "Brake parts must carry the maker's mark.", ["870830"])
    run_id = (await start(CLEAN)).run.run_id
    decide(marking_id, "approve", "notified")

    results = await asyncio.gather(
        resume_compliance(run_id, duty_id), resume_compliance(run_id, marking_id),
        return_exceptions=True,
    )  # fmt: skip

    assert isinstance(results[0], DecisionPendingError)
    assert isinstance(results[1], supervisor.ClearanceRun)
    assert case_run(run_id).awaiting_rule_ids == [duty_id]


def _not_ready(**checklist: Any) -> CaseState:
    items = checklist.pop("items", [])
    return CaseState(
        run_id=str(uuid4()),
        shipment_id=uuid4(),
        as_of=AS_OF,
        data_gaps=checklist.pop("data_gaps", []),
        checklist=Checklist(
            shipment_id=uuid4(),
            container_id="TSTU0000001",
            as_of=AS_OF,
            items=items,
            ready_for_clearance=False,
            summary="not ready",
            **checklist,
        ),
        filing_decision=FilingDecisionRecord(
            id=uuid4(),
            run_id="r",
            shipment_id=uuid4(),
            decision=RuleDecision.APPROVE,
            reviewer=REVIEWER,
            note=None,
            ready_for_clearance=True,
            checklist={},
            decided_at=datetime(2026, 9, 28, 10, tzinfo=UTC),
        ),
    )


class RecordingToolbox:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, server: str, tool: str, arguments: Any = None) -> Any:
        self.calls.append(tool)
        raise AssertionError("the filing must not reach the MCP layer")


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        (_not_ready(data_gaps=["no INNSA tariff for 20rf"],
                    items=[ChecklistItem(key="gap:tariff", source_agent="demurrage_risk",
                                         category="data_gap", severity="blocker",
                                         title="Data gap", detail="no tariff")]),
         "data gap: no INNSA tariff for 20rf"),
        (_not_ready(conflicts=[Conflict(description="documents block, risk says low",
                                        signals=["a", "b"])]),
         "conflict: documents block, risk says low"),
        (_not_ready(missing_inputs=["demurrage_risk"]), "missing input: demurrage_risk"),
    ],
)  # fmt: skip
async def test_filing_an_incomplete_case_never_reaches_mcp(case: CaseState, problem: str) -> None:
    toolbox = RecordingToolbox()
    with pytest.raises(IncompleteCaseError, match=problem):
        await supervisor.file_shipment(case, toolbox)  # type: ignore[arg-type]
    assert toolbox.calls == []


async def test_a_case_with_blockers_ends_blocked_and_cannot_be_approved(seeded: None) -> None:
    run = await start(QTY_MISMATCH)

    assert run.run.state is RunState.BLOCKED and run.waiting_on == []
    assert run.case.outcome == "blocked" and "blocker" in (run.case.outcome_detail or "")
    with pytest.raises(RunAlreadyFinishedError):
        await resume_filing(run.run.run_id, "approve")
    assert events("filing.approved") == []


async def test_a_data_gap_blocks_the_filing(seeded: None) -> None:
    with db_module.session_scope() as session:  # INNSA has no reefer tariff
        created = shipping_service.create_shipment(
            session, make_shipment(container_type="20rf", status=ShipmentStatus.BOOKED), "test"
        )
        for status in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.DOCKED):
            shipping_service.update_status(session, created.id, status, "test")
    run = await supervisor.start_clearance_run(created.id, connector=memory_connector,
                                               model_factory=lambda: None, as_of=AS_OF)  # fmt: skip
    assert run.run.state is RunState.BLOCKED
    assert "data gap" in (run.run.detail or "")


async def test_a_held_shipment_is_not_fileable(seeded: None) -> None:
    run = await start(HELD)
    assert run.run.state is RunState.NOT_FILEABLE
    assert "held" in (run.run.detail or "")


async def test_documents_amended_after_the_approval_stop_the_filing(seeded: None) -> None:
    sid = shipment_id(CLEAN)
    run_id = (await start(CLEAN)).run.run_id
    with db_module.session_scope() as session:
        invoice = next(d for d in shipping_service.get_documents(session, sid)
                       if d.doc_type.value == "invoice")  # fmt: skip
        shipping_service.amend_document(
            session, invoice.id, DocumentAmend(qty=Decimal(1150)), "ops"
        )

    done = await resume_filing(run_id, "approve")

    assert done.run.state is RunState.BLOCKED
    assert "documents changed" in (done.run.detail or "")
    assert status_of(sid) is ShipmentStatus.DOCKED


async def test_one_open_run_per_shipment(seeded: None) -> None:
    run = await start(CLEAN)
    with pytest.raises(RunAlreadyOpenError):
        await start(CLEAN)
    abandoned = supervisor.abandon_clearance_run(run.run.run_id, REVIEWER, "restarting the review")
    assert abandoned.state is RunState.FAILED
    assert (await start(CLEAN)).run.state is RunState.AWAITING_FILING


async def test_checkpoints_hold_only_known_classes(
    seeded: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="langgraph"):
        run = await start(CLEAN)
        restored = await supervisor.inspect_run(run.run.run_id)
    assert "unregistered type" not in caplog.text
    assert restored.case == run.case
    assert ("agents.state", "GateDecision") in checkpoint.checkpoint_classes()


def test_compliance_agent_and_supervisor_keep_to_their_servers() -> None:
    assert supervisor.SERVERS == ("shipping_records_mcp", "compliance_rulebook_mcp")
    assert compliance_rag_agent.SERVERS == ("compliance_rulebook_mcp",)

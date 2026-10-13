"""Phase 10 — the Clearance Checklist agent (fan-in).

Exit criterion: the full JNPT case-study run produces, per shipment, a checklist containing every
discrepancy raised upstream, each attributed to its source agent, with no duplicates. Edge
cases: an upstream agent that raises or times out leaves ``missing_inputs`` and a checklist
from the others; contradictory urgency signals are surfaced as ``conflicts``.
"""

import asyncio
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents import (
    clearance_checklist_agent,
    compliance_rag_agent,
    demurrage_risk_agent,
    document_verification_agent,
    graph,
)
from agents.clearance_checklist_agent import build_checklist
from agents.mcp_clients import memory_connector
from agents.state import AgentFailure, CaseState, ComplianceFinding, Discrepancy, ObservedValue
from config.settings import get_settings
from models.db_models import AuditEvent
from models.enums import ShipmentStatus
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import shipping_service
from services.errors import ShipmentCancelledError

AS_OF = date(2026, 9, 28)
DV, RISK, RAG = (document_verification_agent.AGENT, demurrage_risk_agent.AGENT,
                 compliance_rag_agent.AGENT)  # fmt: skip
QTY_QUESTION = compliance_rag_agent.DISCREPANCY_QUESTIONS["quantity_mismatch"]


def discrepancy(code: str = "quantity_mismatch", field: str = "qty") -> Discrepancy:
    return Discrepancy(code=code, field=field, message=f"{code} found", agent=DV)  # type: ignore[arg-type]


def finding(question: str, found: bool = True) -> ComplianceFinding:
    return ComplianceFinding(question=question, found=found, method="template", agent=RAG,
                             answer="Per [DOC-quantity-consistency v1] ..." if found
                             else compliance_rag_agent.NOT_FOUND)  # fmt: skip


def case(**fields: Any) -> CaseState:
    base: dict[str, Any] = {"run_id": str(uuid4()), "shipment_id": uuid4(), "as_of": AS_OF,
                            "document_outcome": "clear", "risk": "high",
                            "risk_reasons": ["demurrage accruing"]}  # fmt: skip
    return CaseState(**{**base, **fields})


# --------------------------------------------------------------------------- the fan-in (unit)


def test_every_discrepancy_is_one_attributed_item_with_its_guidance() -> None:
    state = case(document_outcome="flagged", document_discrepancies=[discrepancy()],
                 compliance_findings=[finding(QTY_QUESTION), finding("Which rate?")])  # fmt: skip

    checklist = build_checklist(state)

    [doc] = [i for i in checklist.items if i.category == "document"]
    assert (doc.source_agent, doc.severity, doc.title) == (DV, "blocker", "quantity mismatch")
    assert doc.guidance is not None and doc.guidance.startswith("Per [DOC-quantity")
    compliance = [i for i in checklist.items if i.category == "compliance"]
    assert [i.detail.split(" — ")[0] for i in compliance] == ["Which rate?"]  # not repeated
    assert all(i.source_agent == RAG for i in compliance)
    assert checklist.ready_for_clearance is False
    assert checklist.items[0] is doc  # blockers first


def test_duplicate_findings_collapse_into_one_item() -> None:
    state = case(document_outcome="flagged",
                 document_discrepancies=[discrepancy(), discrepancy()])  # fmt: skip

    items = build_checklist(state).items

    assert len([i for i in items if i.category == "document"]) == 1
    assert len({i.key for i in items}) == len(items)


def test_two_findings_about_the_shipment_record_stay_two_items() -> None:
    """No FX rate on the bill of lading date *and* on the invoice date: both are read from the
    shipment record, so the record's value (the date) tells them apart."""

    def no_rate(day: str) -> Discrepancy:
        return Discrepancy(code="fx_rate_unavailable", field="fx_rate", agent=DV,
                           message=f"No USD/INR rate for {day}",
                           observed=[ObservedValue(source="shipment record", value=day)])  # fmt: skip

    state = case(document_outcome="flagged",
                 document_discrepancies=[no_rate("2026-09-10"), no_rate("2026-09-02")])  # fmt: skip

    unavailable = [i for i in build_checklist(state).items if i.title == "fx rate unavailable"]

    assert sorted(i.detail for i in unavailable) == [
        "No USD/INR rate for 2026-09-02", "No USD/INR rate for 2026-09-10"
    ]  # fmt: skip


def test_a_failed_upstream_agent_is_a_missing_input_not_a_failed_checklist() -> None:
    """Edge case: the risk agent raised/timed out → the checklist is built from the other two,
    with missing_inputs = ["demurrage_risk"]."""
    state = case(document_outcome="flagged", document_discrepancies=[discrepancy()],
                 risk=None, risk_reasons=[],
                 agent_failures=[AgentFailure(agent=RISK, kind="timeout",
                                              error="no answer within 0.2 s")])  # fmt: skip

    checklist = build_checklist(state)

    assert checklist.missing_inputs == ["demurrage_risk"]
    assert [i.category for i in checklist.items] == ["document"]
    assert checklist.ready_for_clearance is False
    assert "missing inputs: demurrage_risk" in checklist.summary


def test_contradictory_urgency_signals_are_both_surfaced() -> None:
    """Edge case: documents block clearance but the risk agent says low → a conflict listing
    both signals, not one of them silently preferred."""
    state = case(document_outcome="flagged", document_discrepancies=[discrepancy()],
                 risk="low", risk_reasons=["in free time until 2026-10-02"])  # fmt: skip

    [conflict] = build_checklist(state).conflicts

    assert conflict.signals[0].startswith(f"{DV}: 1 blocking discrepancy (quantity mismatch)")
    assert conflict.signals[1] == f"{RISK}: risk low (in free time until 2026-10-02)"


def test_data_gaps_block_clearance() -> None:
    state = case(data_gaps=["fx_rate: no gazetted USD/INR rate for 2026-11-05"])

    checklist = build_checklist(state)

    [gap] = [i for i in checklist.items if i.category == "data_gap"]
    assert (gap.severity, gap.source_agent, gap.title) == ("blocker", RISK, "data gap: fx_rate")
    assert checklist.ready_for_clearance is False


def test_clean_documents_and_accruing_demurrage_are_ready_but_urgent() -> None:
    checklist = build_checklist(case(compliance_findings=[finding("Which rate?")]))

    assert checklist.ready_for_clearance is True
    assert [(i.category, i.severity) for i in checklist.items] == [
        ("demurrage", "action"), ("compliance", "info")]  # fmt: skip


def test_an_unanswered_rulebook_question_stays_visible() -> None:
    checklist = build_checklist(case(compliance_findings=[finding("Live poultry?", found=False)]))

    [note] = [i for i in checklist.items if i.category == "compliance"]
    assert note.title == "rulebook: not found"
    assert compliance_rag_agent.NOT_FOUND in note.detail


# --------------------------------------------------------------------------- the pipeline


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()


def shipment_id(session_factory: sessionmaker[Session], container_id: str) -> UUID:
    with session_factory() as session:
        return shipping_service.get_shipment_by_container_id(session, container_id).id


async def analyse(sid: UUID) -> CaseState:
    return await graph.analyse_shipment(sid, connector=memory_connector,
                                        model_factory=lambda: None, as_of=AS_OF)  # fmt: skip


async def test_jnpt_case_study_checklists_hold_every_upstream_finding_once(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    """Exit criterion, over all four fixture shipments."""
    for container_id in ("MSKU7001001", "MSCU7002002", "CMAU7003003", "HLXU7004004"):
        state = await analyse(shipment_id(session_factory, container_id))
        checklist = state.checklist
        assert checklist is not None and checklist.container_id == container_id

        documents = [i for i in checklist.items if i.category == "document"]
        assert [i.detail for i in documents] == [d.message for d in state.document_discrepancies]
        assert {i.source_agent for i in documents} <= {DV}
        assert [i.detail for i in checklist.items if i.category == "data_gap"] == state.data_gaps
        [risk] = [i for i in checklist.items if i.category == "demurrage"]
        assert risk.source_agent == RISK and risk.title == f"demurrage risk {state.risk}"
        assert {i.source_agent for i in checklist.items if i.category == "compliance"} <= {RAG}
        assert len({i.key for i in checklist.items}) == len(checklist.items)  # no duplicates
        assert checklist.missing_inputs == [] and checklist.conflicts == []

    flagged = await analyse(shipment_id(session_factory, "MSCU7002002"))
    assert flagged.checklist is not None and flagged.checklist.ready_for_clearance is False
    [qty] = [i for i in flagged.checklist.items if i.category == "document"]
    assert qty.title == "quantity mismatch" and qty.severity == "blocker"
    assert qty.guidance is not None and "DOC-quantity-consistency v1" in qty.references
    held = await analyse(shipment_id(session_factory, "HLXU7004004"))
    assert held.checklist is not None and held.checklist.ready_for_clearance is True


async def test_the_checklist_is_audited_under_its_own_agent(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    state = await analyse(shipment_id(session_factory, "CMAU7003003"))

    with session_factory() as session:
        [event] = session.scalars(select(AuditEvent).where(
            AuditEvent.action == "clearance_checklist.completed")).all()  # fmt: skip
    assert event.actor == clearance_checklist_agent.AGENT
    assert event.entity_id == UUID(state.run_id)
    assert event.payload["ready_for_clearance"] is False


async def test_risk_agent_raising_still_yields_a_checklist(
    seeded: None, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(state: CaseState, config: Any) -> dict[str, Any]:
        raise RuntimeError("tariff service exploded")

    monkeypatch.setattr(demurrage_risk_agent, "demurrage_risk_agent", broken)

    state = await analyse(shipment_id(session_factory, "MSCU7002002"))

    assert state.checklist is not None
    assert state.checklist.missing_inputs == ["demurrage_risk"]
    [failure] = state.agent_failures
    assert (failure.agent, failure.kind) == (RISK, "error")
    assert "RuntimeError: tariff service exploded" in failure.error
    assert any(i.category == "document" for i in state.checklist.items)  # the rest is there
    with session_factory() as session:
        actions = set(session.scalars(select(AuditEvent.action).where(
            AuditEvent.entity_id == UUID(state.run_id))))  # fmt: skip
    assert "agent.failed" in actions


async def test_risk_agent_timing_out_still_yields_a_checklist(
    seeded: None, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stuck(state: CaseState, config: Any) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(demurrage_risk_agent, "demurrage_risk_agent", stuck)
    monkeypatch.setattr(get_settings(), "agent_timeout_seconds", 0.5)

    state = await analyse(shipment_id(session_factory, "MSCU7002002"))

    assert state.checklist is not None
    assert state.checklist.missing_inputs == ["demurrage_risk"]
    assert state.agent_failures[0].kind == "timeout"


async def test_a_cancellation_still_stops_the_whole_pipeline(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = shipment_id(session_factory, "CMAU7003003")
    with session_factory() as session, session.begin():
        shipping_service.update_status(session, sid, ShipmentStatus.CANCELLED, "ops")

    with pytest.raises(ShipmentCancelledError):
        await analyse(sid)

"""The shipment pipeline so far: document verification → (demurrage & risk ‖ compliance) on one
CaseState. (No rulebook is seeded here: the compliance branch runs and finds nothing.)"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents import compliance_rag_agent, demurrage_risk_agent, document_verification_agent, graph
from agents.mcp_clients import memory_connector
from models.db_models import AuditEvent
from models.enums import ShipmentStatus
from scripts import seed_fx_rates, seed_port_rates, seed_shipments
from services import shipping_service
from services.errors import ShipmentCancelledError

AS_OF = date(2026, 9, 28)


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()


def shipment_id(session_factory: sessionmaker[Session], container_id: str) -> UUID:
    with session_factory() as session:
        return shipping_service.get_shipment_by_container_id(session, container_id).id


async def test_pipeline_fills_both_slices_and_risk_sees_the_discrepancies(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = shipment_id(session_factory, "MSCU7002002")

    with structlog.testing.capture_logs() as logs:
        state = await graph.analyse_shipment(sid, connector=memory_connector,
                                             model_factory=lambda: None, as_of=AS_OF)  # fmt: skip

    assert state.document_outcome == "flagged"
    assert state.demurrage_estimate is not None
    assert state.demurrage_estimate.amount_inr == Decimal("15912.00")
    assert state.risk == "high"
    assert state.risk_reasons[-1].startswith("1 document discrepancy(ies) (quantity_mismatch)")
    # each agent calls under its own name, on its own servers; one run_id threads through all
    agents = {a.AGENT: a for a in (document_verification_agent, demurrage_risk_agent,
                                   compliance_rag_agent)}  # fmt: skip
    calls = [e for e in logs if e["event"] == "agent.tool_call"]
    assert {e["actor"] for e in calls} == set(agents)
    assert {e["run_id"] for e in calls} == {state.run_id}
    for e in calls:
        assert e["server"] in agents[e["actor"]].SERVERS
    with session_factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "agent_run")
            .order_by(AuditEvent.created_at)
        ).all()  # fmt: skip
    # verification first; then risk and compliance (parallel branches, either order)
    assert events[0].action == "document_verification.completed"
    assert {e.action for e in events[1:]} == {"demurrage_risk.completed",
                                               "compliance_rag.completed"}  # fmt: skip
    assert {e.entity_id for e in events} == {UUID(state.run_id)}


async def test_a_clean_shipment_carries_no_document_reason(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    state = await graph.analyse_shipment(shipment_id(session_factory, "MSKU7001001"),
                                         connector=memory_connector, model_factory=lambda: None,
                                         as_of=AS_OF)  # fmt: skip

    assert state.document_outcome == "clear"
    assert not any("document" in r for r in state.risk_reasons)


async def test_a_cancelled_shipment_stops_the_pipeline_at_the_first_node(
    seeded: None, session_factory: sessionmaker[Session]
) -> None:
    sid = shipment_id(session_factory, "CMAU7003003")
    with session_factory() as session, session.begin():
        shipping_service.update_status(session, sid, ShipmentStatus.CANCELLED, "ops")

    with pytest.raises(ShipmentCancelledError):
        await graph.analyse_shipment(sid, connector=memory_connector, model_factory=lambda: None,
                                     as_of=AS_OF)  # fmt: skip

    with session_factory() as session:
        actions = list(session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_type == "agent_run")))  # fmt: skip
    assert actions == ["document_verification.stopped"]

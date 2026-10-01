"""Phase 12 — audit_service.query_events: reading the trail back for Mission Control."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from models.schemas import AuditEventRecord
from services import audit_service as svc

RUN = "11111111-1111-1111-1111-111111111111"
OTHER_RUN = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def trail(db_session: Session) -> UUID:
    shipment = uuid4()
    rows: list[tuple[str, str, str, UUID, dict[str, Any]]] = [
        ("document_verification_agent", "document_verification.completed", "agent_run",
         UUID(RUN), {"outcome": "flagged"}),
        ("supervisor", "case_run.state_changed", "shipment", shipment,
         {"run_id": RUN, "to": "awaiting_filing"}),
        ("Asha Rao", "filing.approved", "shipment", shipment, {"run_id": RUN}),
        ("ops", "shipment.updated", "shipment", shipment, {"changes": {}}),
        ("demurrage_risk_agent", "demurrage_risk.completed", "agent_run", UUID(OTHER_RUN), {}),
    ]  # fmt: skip
    for actor, action, entity_type, entity_id, payload in rows:
        svc.record(db_session, actor=actor, action=action, entity_type=entity_type,
                   entity_id=entity_id, payload=payload)  # fmt: skip
        db_session.flush()
    return shipment


def actions(events: list[AuditEventRecord]) -> set[str]:
    return {e.action for e in events}


def test_a_run_is_its_agent_run_events_and_events_naming_it(
    trail: UUID, db_session: Session
) -> None:
    assert actions(svc.query_events(db_session, run_id=RUN)) == {
        "document_verification.completed", "case_run.state_changed", "filing.approved"
    }  # fmt: skip
    assert actions(svc.query_events(db_session, run_id=OTHER_RUN)) == {"demurrage_risk.completed"}
    assert svc.query_events(db_session, run_id="not-a-uuid") == []


def test_filters_combine(trail: UUID, db_session: Session) -> None:
    assert actions(svc.query_events(db_session, actor="Asha Rao")) == {"filing.approved"}
    assert actions(svc.query_events(db_session, action_prefix="filing.")) == {"filing.approved"}
    assert actions(svc.query_events(db_session, action_prefix="%")) == set()  # escaped
    assert len(svc.query_events(db_session, entity_type="shipment", entity_id=trail)) == 3
    assert actions(svc.query_events(db_session, entity_type="agent_run",
                                    action="demurrage_risk.completed")) == {
        "demurrage_risk.completed"}  # fmt: skip


def test_newest_first_limited_and_time_bounded(trail: UUID, db_session: Session) -> None:
    events = svc.query_events(db_session)
    assert len(events) == 5
    assert [e.created_at for e in events] == sorted((e.created_at for e in events), reverse=True)
    assert len(svc.query_events(db_session, limit=2)) == 2
    future = datetime.now(UTC) + timedelta(days=1)
    assert svc.query_events(db_session, since=future) == []
    assert len(svc.query_events(db_session, until=future)) == 5


def test_filter_choices(trail: UUID, db_session: Session) -> None:
    assert svc.list_actors(db_session) == sorted(
        ["Asha Rao", "demurrage_risk_agent", "document_verification_agent", "ops", "supervisor"]
    )
    assert svc.list_entity_types(db_session) == ["agent_run", "shipment"]

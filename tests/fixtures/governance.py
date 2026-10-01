"""Helpers for tests that need a shipment filed (``cleared``) without running the supervisor.

Since Phase 11 a shipment reaches ``cleared`` only through ``shipping_service.clear_shipment``
citing an approved filing decision of a clearance run — these helpers create that trail
directly through the services, as the supervisor would.
"""

from datetime import date
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from models.enums import RunState, ShipmentStatus
from models.schemas import FilingDecisionRecord, ShipmentRecord
from services import governance_service, shipping_service

REVIEWER = "Asha Rao"  # registered in tests/conftest.py (TEST_REVIEWERS)


def approve_filing(session: Session, shipment_id: UUID) -> FilingDecisionRecord:
    """An open clearance run for the shipment, waiting at the filing gate, approved."""
    run_id = str(uuid4())
    governance_service.start_run(session, run_id, shipment_id, date(2026, 9, 28), "test")
    governance_service.update_run(session, run_id, RunState.AWAITING_FILING, actor="test")
    return governance_service.record_filing_decision(
        session,
        run_id,
        "approve",
        REVIEWER,
        None,
        checklist={"ready_for_clearance": True},
        ready_for_clearance=True,
    )


def clear(session: Session, shipment_id: UUID, actor: str = "test") -> ShipmentRecord:
    """File an ``under_review`` shipment: record an approval, then ``clear_shipment``."""
    decision = approve_filing(session, shipment_id)
    cleared = shipping_service.clear_shipment(session, shipment_id, decision.id, actor)
    assert cleared.status is ShipmentStatus.CLEARED
    governance_service.update_run(session, decision.run_id, RunState.FILED, actor="test")
    return cleared

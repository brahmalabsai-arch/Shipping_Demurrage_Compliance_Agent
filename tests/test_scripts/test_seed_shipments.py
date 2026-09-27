"""Phase 1 — the seed script creates the four fixtures once and is safe to re-run."""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from models.db_models import AuditEvent, Document, Shipment
from models.enums import ShipmentStatus
from models.schemas import ShipmentUpdate
from scripts.seed_shipments import seed
from services import shipping_service
from tests.fixtures.shipments import SEED_SHIPMENTS


def _counts(session: Session) -> tuple[int | None, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (Shipment, Document, AuditEvent)
    )


def test_seed_creates_the_four_fixtures_in_their_target_states(
    session_factory: sessionmaker[Session],
) -> None:
    results = seed()

    assert [r.outcome for r in results] == ["created"] * 4
    with session_factory() as session:
        by_key = {
            f.key: shipping_service.get_shipment_by_container_id(session, f.shipment.container_id)
            for f in SEED_SHIPMENTS
        }
        assert {k: s.status for k, s in by_key.items()} == {
            "clean": ShipmentStatus.DOCKED,
            "qty_mismatch": ShipmentStatus.DOCKED,
            "missing_document": ShipmentStatus.IN_TRANSIT,
            "already_delayed": ShipmentStatus.HELD,
        }
        doc_types = {
            k: sorted(d.doc_type for d in shipping_service.get_documents(session, s.id))
            for k, s in by_key.items()
        }
        assert "invoice" not in doc_types["missing_document"]
        assert "invoice" in doc_types["clean"]


def test_seed_is_idempotent_and_preserves_later_edits(
    session_factory: sessionmaker[Session],
) -> None:
    seed()
    with session_factory() as session, session.begin():
        clean = shipping_service.get_shipment_by_container_id(session, "MSKU7001001")
        shipping_service.update_shipment(
            session, clean.id, ShipmentUpdate(declared_qty=Decimal("1199")), "dashboard-user"
        )
    with session_factory() as session:
        counts_before = _counts(session)

    results = seed()

    assert [r.outcome for r in results] == ["skipped"] * 4
    with session_factory() as session:
        assert _counts(session) == counts_before
        edited = shipping_service.get_shipment_by_container_id(session, "MSKU7001001")
        assert edited.declared_qty == Decimal("1199")

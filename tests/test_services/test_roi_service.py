"""Phase 12 — roi_service: named, append-only, integrity-checked ROI snapshots."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import AuditEvent, RoiSnapshot
from models.schemas import RoiAssumptions
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import roi_service as svc
from services.errors import (
    DuplicateSnapshotNameError,
    InvalidAssumptionsError,
    RoiSnapshotNotFoundError,
    SnapshotIntegrityError,
)

AS_OF = date(2026, 9, 28)


@pytest.fixture
def seeded(db_session: Session) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()


def test_capture_freezes_what_prices_each_shipment(seeded: None, db_session: Session) -> None:
    snapshot = svc.capture_snapshot(db_session, " fixtures ", AS_OF, RoiAssumptions(), "ops")

    assert (snapshot.name, snapshot.as_of, snapshot.captured_by) == ("fixtures", AS_OF, "ops")
    items = {s.shipment.container_id: s for s in snapshot.content.shipments}
    assert set(items) == {"MSKU7001001", "MSCU7002002", "CMAU7003003", "HLXU7004004"}
    cmau = items["CMAU7003003"]
    assert cmau.arrival_date == date(2026, 10, 5)
    assert cmau.tariff is not None and cmau.tariff.daily_rate == Decimal("110.00")
    assert cmau.fx is not None and cmau.fx.rate == Decimal("88.400000") and cmau.gaps == []
    assert [d.doc_type.value for d in cmau.documents] == ["packing_list", "bill_of_lading"]
    assert len(snapshot.content.rules_in_force) == 6
    assert snapshot.content_sha256 == svc.sha256_of(snapshot.content.model_dump(mode="json"))
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "roi_snapshot.captured")
    ).one()
    assert (
        event.entity_id == snapshot.id
        and event.payload["content_sha256"] == snapshot.content_sha256
    )


def test_snapshots_are_read_back_unchanged(seeded: None, db_session: Session) -> None:
    captured = svc.capture_snapshot(db_session, "a", AS_OF, RoiAssumptions(), "ops")
    db_session.expire_all()

    assert svc.get_snapshot(db_session, captured.id) == captured
    assert svc.get_snapshot_by_name(db_session, "a") == captured
    assert [s.name for s in svc.list_snapshots(db_session)] == ["a"]
    with pytest.raises(RoiSnapshotNotFoundError):
        svc.get_snapshot_by_name(db_session, "nope")


def test_names_are_unique_and_snapshots_append_only(seeded: None, db_session: Session) -> None:
    captured = svc.capture_snapshot(db_session, "a", AS_OF, RoiAssumptions(), "ops")
    with pytest.raises(DuplicateSnapshotNameError):
        svc.capture_snapshot(db_session, "a", AS_OF, RoiAssumptions(), "ops")
    for statement in ("UPDATE roi.snapshots SET note = 'x'", "DELETE FROM roi.snapshots"):
        with pytest.raises(IntegrityError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(statement))
    assert svc.get_snapshot(db_session, captured.id).note is None


def test_an_altered_snapshot_is_refused(seeded: None, db_session: Session) -> None:
    captured = svc.capture_snapshot(db_session, "a", AS_OF, RoiAssumptions(), "ops")
    row = db_session.get_one(RoiSnapshot, captured.id)
    row.content = {**row.content, "as_of": "2026-01-01"}  # in memory only — never flushed

    with pytest.raises(SnapshotIntegrityError):
        svc._record(row)
    db_session.expunge(row)


@pytest.mark.parametrize(
    "assumptions",
    [
        RoiAssumptions(hold_days={"missing_document": 7}),  # incomplete
        RoiAssumptions(hold_days={**RoiAssumptions().hold_days, "invented": 3}),  # unknown
    ],
)
def test_assumptions_must_cover_every_issue_type(
    seeded: None, db_session: Session, assumptions: RoiAssumptions
) -> None:
    with pytest.raises(InvalidAssumptionsError):
        svc.capture_snapshot(db_session, "x", AS_OF, assumptions, "ops")


def test_a_blank_name_is_refused(seeded: None, db_session: Session) -> None:
    with pytest.raises(InvalidAssumptionsError, match="name"):
        svc.capture_snapshot(db_session, "  ", AS_OF, RoiAssumptions(), "ops")


def test_gaps_are_frozen_not_guessed(seeded: None, db_session: Session) -> None:
    """After the last gazetted period (10-31) there is no FX rate: the gap is recorded."""
    snapshot = svc.capture_snapshot(db_session, "late", date(2026, 12, 1), RoiAssumptions(), "ops")
    for item in snapshot.content.shipments:
        assert item.fx is None and any(g.startswith("fx: ") for g in item.gaps)

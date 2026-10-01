"""ROI snapshots (Phase 12): the frozen inputs of a before/after ROI comparison.

``capture_snapshot`` reads the live data once — every shipment that is not cancelled with its
current documents, the tariff in force on its arrival day and the gazetted port-charges FX rate
on the analysis date (the Phase 8 demurrage conventions), plus the rulebook in force — and
stores it with the scenario assumptions as a named, append-only ``roi.snapshots`` row. The ROI
calculator (``roi.calculator``) only ever reads snapshots, so editing rates, rules or documents
afterwards cannot change a comparison (edge_cases.md Phase 12; conventions §9).

Whatever cannot be frozen (no ETA, no tariff for the container type, no gazetted rate) is
recorded as a gap on that shipment — never guessed.
"""

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import RoiSnapshot
from models.enums import FxStage, ShipmentStatus
from models.schemas import (
    DEFAULT_HOLD_DAYS,
    PortRateRecord,
    ResolvedRate,
    RoiAssumptions,
    RoiSnapshotContent,
    RoiSnapshotRecord,
    ShipmentRecord,
    SnapshotShipment,
)
from services import (
    audit_service,
    compliance_service,
    demurrage_service,
    fx_service,
    shipping_service,
)
from services.errors import (
    DuplicateSnapshotNameError,
    InvalidAssumptionsError,
    PortRateNotFoundError,
    RateNotFoundError,
    RoiSnapshotNotFoundError,
    SnapshotIntegrityError,
)

IST = ZoneInfo("Asia/Kolkata")  # the arrival day is the IST day of the ETA (Phase 8)


def sha256_of(content: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a snapshot's content (key order and spacing fixed)."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_assumptions(assumptions: RoiAssumptions) -> None:
    """Every issue type the cross-check can raise needs a hold duration, and only those."""
    missing = sorted(set(DEFAULT_HOLD_DAYS) - set(assumptions.hold_days))
    unknown = sorted(set(assumptions.hold_days) - set(DEFAULT_HOLD_DAYS))
    if missing or unknown:
        raise InvalidAssumptionsError(
            f"hold_days must give a duration for each issue type; missing {missing}, "
            f"unknown {unknown}"
        )


def _freeze(session: Session, shipment: ShipmentRecord, as_of: date) -> SnapshotShipment:
    documents = shipping_service.get_documents(session, shipment.id)
    gaps: list[str] = []
    arrival = shipment.eta.astimezone(IST).date() if shipment.eta else None
    tariff: PortRateRecord | None = None
    fx: ResolvedRate | None = None
    if arrival is None:
        gaps.append("eta: not set, so the arrival day (and its tariff) is unknown")
    else:
        try:
            tariff = demurrage_service.get_rate(
                session, shipment.destination_port, shipment.container_type, arrival
            )
        except PortRateNotFoundError as exc:
            gaps.append(f"tariff: {exc}")
    if tariff is not None and tariff.currency != "INR":
        try:
            fx = fx_service.resolve_rate(
                session, f"{tariff.currency}/INR", as_of, FxStage.PORT_CHARGES
            )
        except RateNotFoundError as exc:
            gaps.append(f"fx: {exc}")
    return SnapshotShipment(
        shipment=shipment,
        documents=documents,
        arrival_date=arrival,
        tariff=tariff,
        fx=fx,
        gaps=gaps,
    )


def capture_snapshot(
    session: Session,
    name: str,
    as_of: date,
    assumptions: RoiAssumptions,
    actor: str,
    note: str | None = None,
) -> RoiSnapshotRecord:
    """Freeze the live data into a new named snapshot (audited ``roi_snapshot.captured``).

    Raises ``DuplicateSnapshotNameError`` for a name already used (snapshots are never
    overwritten) and ``InvalidAssumptionsError`` for incomplete assumptions.
    """
    name = name.strip()
    if not name:
        raise InvalidAssumptionsError("a snapshot needs a name")
    check_assumptions(assumptions)
    shipments = [
        s
        for s in shipping_service.list_shipments(session)
        if s.status is not ShipmentStatus.CANCELLED
    ]
    content = RoiSnapshotContent(
        as_of=as_of,
        assumptions=assumptions,
        shipments=[_freeze(session, s, as_of) for s in shipments],
        rules_in_force=compliance_service.list_approved(session),
    )
    data = content.model_dump(mode="json")
    row = RoiSnapshot(
        name=name,
        as_of=as_of,
        captured_by=actor,
        note=(note or "").strip() or None,
        content=data,
        content_sha256=sha256_of(data),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "uq_snapshots_name" in str(exc.orig):
            raise DuplicateSnapshotNameError(f"an ROI snapshot named {name!r} exists") from exc
        raise
    session.refresh(row)
    audit_service.record(
        session,
        actor=actor,
        action="roi_snapshot.captured",
        entity_type="roi_snapshot",
        entity_id=row.id,
        payload={
            "name": name,
            "as_of": as_of,
            "content_sha256": row.content_sha256,
            "shipments": [s.shipment.container_id for s in content.shipments],
            "gaps": {s.shipment.container_id: s.gaps for s in content.shipments if s.gaps},
            "assumptions": assumptions.model_dump(),
        },
    )
    session.flush()
    return _record(row)


def _record(row: RoiSnapshot) -> RoiSnapshotRecord:
    if sha256_of(row.content) != row.content_sha256:
        raise SnapshotIntegrityError(f"ROI snapshot {row.name!r} does not match its SHA-256")
    return RoiSnapshotRecord(
        id=row.id,
        name=row.name,
        as_of=row.as_of,
        captured_by=row.captured_by,
        note=row.note,
        content=RoiSnapshotContent.model_validate(row.content),
        content_sha256=row.content_sha256,
        captured_at=row.captured_at,
    )


def get_snapshot(session: Session, snapshot_id: UUID) -> RoiSnapshotRecord:
    """One snapshot by id, its content verified against its SHA-256."""
    row = session.get(RoiSnapshot, snapshot_id)
    if row is None:
        raise RoiSnapshotNotFoundError(f"no ROI snapshot with id {snapshot_id}")
    return _record(row)


def get_snapshot_by_name(session: Session, name: str) -> RoiSnapshotRecord:
    """One snapshot by its name."""
    row = session.scalars(select(RoiSnapshot).where(RoiSnapshot.name == name.strip())).first()
    if row is None:
        raise RoiSnapshotNotFoundError(f"no ROI snapshot named {name!r}")
    return _record(row)


def list_snapshots(session: Session) -> list[RoiSnapshotRecord]:
    """Every snapshot, newest first."""
    rows = session.scalars(select(RoiSnapshot).order_by(RoiSnapshot.captured_at.desc()))
    return [_record(row) for row in rows]

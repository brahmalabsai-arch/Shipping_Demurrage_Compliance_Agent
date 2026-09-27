"""Shipping records: shipments, their documents and lifecycle status.

The single code path to shipping data for both the Shipping Ops dashboard and
``shipping_records_mcp`` (architecture §4). Functions take the caller's ``Session`` and only
flush — the caller's :func:`services.db.session_scope` commits the change together with its
``audit.events`` row. Nothing is ever deleted: cancellation is a status and document amendments
chain through ``superseded_by`` (conventions §3).
"""

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import Document, Shipment
from models.enums import ShipmentStatus
from models.schemas import (
    DocumentAmend,
    DocumentCreate,
    DocumentRecord,
    ShipmentCreate,
    ShipmentRecord,
    ShipmentUpdate,
)
from services import audit_service
from services.errors import (
    DocumentNotFoundError,
    DocumentSupersededError,
    DuplicateContainerError,
    InvalidShipmentDataError,
    InvalidStatusTransitionError,
    ShipmentCancelledError,
    ShipmentNotFoundError,
)

S = ShipmentStatus

# Allowed lifecycle moves. `held` is a customs hold that returns to review once resolved;
# cancellation is only possible before the cargo reaches port. Reaching `cleared` will be
# additionally gated on an explicit human approval by the Phase 11 supervisor.
TRANSITIONS: dict[ShipmentStatus, frozenset[ShipmentStatus]] = {
    S.QUOTED: frozenset({S.BOOKED, S.CANCELLED}),
    S.BOOKED: frozenset({S.IN_TRANSIT, S.CANCELLED}),
    S.IN_TRANSIT: frozenset({S.DOCKED, S.CANCELLED}),
    S.DOCKED: frozenset({S.UNDER_REVIEW, S.HELD}),
    S.UNDER_REVIEW: frozenset({S.CLEARED, S.HELD}),
    S.HELD: frozenset({S.UNDER_REVIEW}),
    S.CLEARED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
    S.CANCELLED: frozenset(),
}


def allowed_transitions(status: ShipmentStatus) -> frozenset[ShipmentStatus]:
    """Statuses reachable in one step, so UIs only offer legal moves."""
    return TRANSITIONS[status]


# --------------------------------------------------------------------------- shipments


def create_shipment(session: Session, data: ShipmentCreate, actor: str) -> ShipmentRecord:
    """Register a new shipment (as a quotation or a booking) and audit its creation.

    Raises ``DuplicateContainerError`` if the container is already tracked — surfaced from the
    database's unique constraint so concurrent creates cannot both succeed.
    """
    row = Shipment(**data.model_dump())
    row.quote_expiry_date = _quote_expiry(row.quote_date, row.quote_validity_days)
    try:
        with session.begin_nested():  # a failed insert must not poison the caller's transaction
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if "uq_shipments_container_id" in str(exc.orig):
            raise DuplicateContainerError(f"container {data.container_id} already exists") from exc
        raise

    record = ShipmentRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="shipment.created",
        entity_type="shipment",
        entity_id=row.id,
        payload={"shipment": record.model_dump()},
    )
    session.flush()
    return record


def get_shipment(session: Session, shipment_id: UUID) -> ShipmentRecord:
    """Fetch one shipment by id, in any status (including cancelled)."""
    return ShipmentRecord.model_validate(_load_shipment(session, shipment_id))


def get_shipment_by_container_id(session: Session, container_id: str) -> ShipmentRecord:
    """Fetch one shipment by its container number — the identifier operators actually use."""
    row = session.scalars(select(Shipment).where(Shipment.container_id == container_id)).first()
    if row is None:
        raise ShipmentNotFoundError(f"no shipment with container_id {container_id}")
    return ShipmentRecord.model_validate(row)


def get_active_shipment(session: Session, shipment_id: UUID) -> ShipmentRecord:
    """Fetch a shipment for analysis, refusing cancelled ones.

    Agents call this at read time so a pipeline that started before a cancellation stops with
    ``ShipmentCancelledError`` instead of completing work on a dead shipment.
    """
    record = get_shipment(session, shipment_id)
    if record.status is ShipmentStatus.CANCELLED:
        raise ShipmentCancelledError(f"shipment {record.container_id} is cancelled")
    return record


def list_shipments(session: Session, status: ShipmentStatus | None = None) -> list[ShipmentRecord]:
    """All shipments, optionally only those in one lifecycle status, oldest departure first."""
    query = select(Shipment).order_by(Shipment.etd, Shipment.container_id)
    if status is not None:
        query = query.where(Shipment.status == status)
    return [ShipmentRecord.model_validate(row) for row in session.scalars(query)]


def update_shipment(
    session: Session, shipment_id: UUID, changes: ShipmentUpdate, actor: str
) -> ShipmentRecord:
    """Apply a partial edit to a shipment's descriptive fields and audit the before/after diff.

    Only fields explicitly set on ``changes`` are applied. Cross-field rules are re-validated on
    the merged row (``InvalidShipmentDataError``); cancelled shipments are frozen
    (``ShipmentCancelledError``).
    """
    row = _load_shipment(session, shipment_id)
    _ensure_not_cancelled(row)

    updates = changes.model_dump(exclude_unset=True)
    _validate_merged(row, updates)  # before touching the row, so a rejected edit changes nothing
    diff = _apply(row, updates)
    row.quote_expiry_date = _quote_expiry(row.quote_date, row.quote_validity_days)
    if not diff:
        return ShipmentRecord.model_validate(row)

    session.flush()
    audit_service.record(
        session,
        actor=actor,
        action="shipment.updated",
        entity_type="shipment",
        entity_id=row.id,
        payload={"changes": diff},
    )
    session.flush()
    return ShipmentRecord.model_validate(row)


def update_status(
    session: Session, shipment_id: UUID, status: ShipmentStatus, actor: str
) -> ShipmentRecord:
    """Move a shipment one step along its lifecycle, enforcing :data:`TRANSITIONS`.

    Raises ``InvalidStatusTransitionError`` for any move not in the table, including a no-op
    move to the current status.
    """
    row = _load_shipment(session, shipment_id)
    current = row.status
    if status not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "none (terminal)"
        raise InvalidStatusTransitionError(
            f"{row.container_id}: cannot move {current} -> {status}; allowed: {allowed}"
        )

    row.status = status
    session.flush()
    audit_service.record(
        session,
        actor=actor,
        action="shipment.status_changed",
        entity_type="shipment",
        entity_id=row.id,
        payload={"from": current, "to": status},
    )
    session.flush()
    return ShipmentRecord.model_validate(row)


# --------------------------------------------------------------------------- documents


def add_document(
    session: Session, shipment_id: UUID, data: DocumentCreate, actor: str
) -> DocumentRecord:
    """Attach a new document (version 1 of its own chain) to a shipment."""
    shipment = _load_shipment(session, shipment_id)
    _ensure_not_cancelled(shipment)

    row = Document(shipment_id=shipment.id, version=1, **data.model_dump())
    session.add(row)
    session.flush()
    record = DocumentRecord.model_validate(row)
    audit_service.record(
        session,
        actor=actor,
        action="document.added",
        entity_type="document",
        entity_id=row.id,
        payload={"document": record.model_dump()},
    )
    session.flush()
    return record


def amend_document(
    session: Session, document_id: UUID, changes: DocumentAmend, actor: str
) -> DocumentRecord:
    """Re-issue a document: write version n+1 and point version n at it via ``superseded_by``.

    The old version is kept untouched (no in-place edits, no deletes) so the history of what
    was declared stays auditable. Only the latest version may be amended.
    """
    old = session.get(Document, document_id)
    if old is None:
        raise DocumentNotFoundError(f"no document {document_id}")
    if old.superseded_by is not None:
        raise DocumentSupersededError(
            f"document {document_id} (v{old.version}) is superseded by {old.superseded_by}"
        )
    _ensure_not_cancelled(_load_shipment(session, old.shipment_id))

    carried = {
        "qty": old.qty,
        "value": old.value,
        "currency": old.currency,
        "issued_date": old.issued_date,
    }
    carried.update(changes.model_dump(exclude_unset=True))
    new = Document(
        shipment_id=old.shipment_id, doc_type=old.doc_type, version=old.version + 1, **carried
    )
    session.add(new)
    session.flush()
    old.superseded_by = new.id
    session.flush()

    record = DocumentRecord.model_validate(new)
    audit_service.record(
        session,
        actor=actor,
        action="document.amended",
        entity_type="document",
        entity_id=new.id,
        payload={"supersedes": old.id, "document": record.model_dump()},
    )
    session.flush()
    return record


def get_documents(
    session: Session, shipment_id: UUID, include_superseded: bool = False
) -> list[DocumentRecord]:
    """A shipment's documents — by default only the current (non-superseded) versions, which is
    what verification should compare."""
    _load_shipment(session, shipment_id)
    query = (
        select(Document)
        .where(Document.shipment_id == shipment_id)
        .order_by(Document.doc_type, Document.issued_date, Document.version)
    )
    if not include_superseded:
        query = query.where(Document.superseded_by.is_(None))
    return [DocumentRecord.model_validate(row) for row in session.scalars(query)]


# --------------------------------------------------------------------------- helpers


def _load_shipment(session: Session, shipment_id: UUID) -> Shipment:
    row = session.get(Shipment, shipment_id)
    if row is None:
        raise ShipmentNotFoundError(f"no shipment {shipment_id}")
    return row


def _ensure_not_cancelled(row: Shipment) -> None:
    if row.status is ShipmentStatus.CANCELLED:
        raise ShipmentCancelledError(f"shipment {row.container_id} is cancelled")


def _quote_expiry(quote_date: date | None, validity_days: int | None) -> date | None:
    if quote_date is None or validity_days is None:
        return None
    return quote_date + timedelta(days=validity_days)


def _apply(row: Shipment, updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    diff: dict[str, dict[str, Any]] = {}
    for field, new_value in updates.items():
        old_value = getattr(row, field)
        if old_value != new_value:
            setattr(row, field, new_value)
            diff[field] = {"from": old_value, "to": new_value}
    return diff


def _validate_merged(row: Shipment, updates: dict[str, Any]) -> None:
    def merged(field: str) -> Any:
        return updates.get(field, getattr(row, field))

    if (merged("quote_date") is None) != (merged("quote_validity_days") is None):
        raise InvalidShipmentDataError("quote_date and quote_validity_days must be set together")
    eta, etd = merged("eta"), merged("etd")
    if eta is not None and eta < etd:
        raise InvalidShipmentDataError("eta cannot be before etd")

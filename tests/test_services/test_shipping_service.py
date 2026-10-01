"""Phase 1 — shipping_service: CRUD, status transitions, documents, audit, edge cases."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import AuditEvent
from models.enums import ContainerType, DocType, RunState, ShipmentStatus
from models.schemas import (
    DocumentAmend,
    DocumentCreate,
    ShipmentCreate,
    ShipmentRecord,
    ShipmentUpdate,
)
from services import governance_service
from services import shipping_service as svc
from services.errors import (
    ClearanceRequiresApprovalError,
    DocumentNotFoundError,
    DocumentSupersededError,
    DuplicateContainerError,
    FilingDecisionNotFoundError,
    InvalidFilingDecisionError,
    InvalidShipmentDataError,
    InvalidStatusTransitionError,
    ShipmentCancelledError,
    ShipmentNotFoundError,
)
from tests.fixtures.governance import approve_filing, clear
from tests.fixtures.shipments import make_shipment

S = ShipmentStatus
ACTOR = "test-operator"


def _events(session: Session, entity_id: uuid.UUID) -> list[AuditEvent]:
    query = (
        select(AuditEvent).where(AuditEvent.entity_id == entity_id).order_by(AuditEvent.created_at)
    )
    return list(session.scalars(query))


def _invoice(**kw: object) -> DocumentCreate:
    fields: dict[str, object] = {
        "doc_type": DocType.INVOICE,
        "issued_date": date(2026, 8, 18),
        "qty": Decimal("100"),
        "value": Decimal("5000.00"),
        "currency": "USD",
    }
    fields.update(kw)
    return DocumentCreate.model_validate(fields)


# --------------------------------------------------------------------------- create / read


def test_create_shipment_persists_record_and_computes_quote_expiry(db_session: Session) -> None:
    created = svc.create_shipment(
        db_session, make_shipment(quote_date=date(2026, 8, 10), quote_validity_days=14), ACTOR
    )

    assert created.status is S.QUOTED
    assert created.quote_expiry_date == date(2026, 8, 24)
    assert created.created_at.tzinfo is not None
    assert svc.get_shipment(db_session, created.id) == created


def test_create_shipment_writes_audit_event_in_same_transaction(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)

    [event] = _events(db_session, created.id)
    assert (event.actor, event.action, event.entity_type) == (ACTOR, "shipment.created", "shipment")
    assert event.payload["shipment"]["container_id"] == "TSTU0000001"


def test_duplicate_container_id_raises_typed_error_and_keeps_session_usable(
    db_session: Session,
) -> None:
    svc.create_shipment(db_session, make_shipment(container_id="DUPU1234567"), ACTOR)

    with pytest.raises(DuplicateContainerError):
        svc.create_shipment(db_session, make_shipment(container_id="DUPU1234567"), ACTOR)

    # The failed insert was isolated in a savepoint: the transaction can carry on.
    other = svc.create_shipment(db_session, make_shipment(container_id="OKAU7654321"), ACTOR)
    assert svc.get_shipment_by_container_id(db_session, "OKAU7654321") == other


@pytest.mark.parametrize(
    "overrides",
    [
        {"declared_qty": Decimal("-1")},
        {"declared_currency": "usd"},
        {"container_id": "BAD-ID"},
        {"hs_code": "87.08"},
        {"status": S.DOCKED},  # new shipments start as quoted or booked only
        {"quote_date": None, "quote_validity_days": None},  # quoted needs a quote
        {"quote_validity_days": 30},  # validity window is 7-14 days
    ],
)
def test_create_rejects_invalid_input_at_the_pydantic_boundary(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        make_shipment(**overrides)


def test_booked_shipment_needs_no_quote(db_session: Session) -> None:
    created = svc.create_shipment(
        db_session,
        make_shipment(status=S.BOOKED, quote_date=None, quote_validity_days=None),
        ACTOR,
    )
    assert (created.status, created.quote_expiry_date) == (S.BOOKED, None)


def test_unknown_shipment_raises_not_found(db_session: Session) -> None:
    with pytest.raises(ShipmentNotFoundError):
        svc.get_shipment(db_session, uuid.uuid4())
    with pytest.raises(ShipmentNotFoundError):
        svc.get_shipment_by_container_id(db_session, "NONE0000000")


def test_list_shipments_filters_by_status(db_session: Session) -> None:
    quoted = svc.create_shipment(db_session, make_shipment(container_id="AAAU0000001"), ACTOR)
    booked = svc.create_shipment(db_session, make_shipment(container_id="BBBU0000002"), ACTOR)
    svc.update_status(db_session, booked.id, S.BOOKED, ACTOR)

    assert [s.id for s in svc.list_shipments(db_session, S.QUOTED)] == [quoted.id]
    assert [s.id for s in svc.list_shipments(db_session, S.BOOKED)] == [booked.id]
    assert {s.id for s in svc.list_shipments(db_session)} == {quoted.id, booked.id}


# --------------------------------------------------------------------------- update


def test_update_applies_only_explicitly_set_fields_and_audits_the_diff(
    db_session: Session,
) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)

    updated = svc.update_shipment(
        db_session, created.id, ShipmentUpdate(declared_qty=Decimal("1150")), ACTOR
    )

    assert updated.declared_qty == Decimal("1150")
    assert updated.declared_value == created.declared_value  # untouched
    assert updated.updated_at >= created.updated_at
    event = _events(db_session, created.id)[-1]
    assert event.action == "shipment.updated"
    assert event.payload == {"changes": {"declared_qty": {"from": "1200", "to": "1150"}}}


def test_update_can_clear_nullable_eta(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)

    updated = svc.update_shipment(db_session, created.id, ShipmentUpdate(eta=None), ACTOR)

    assert updated.eta is None


def test_update_recomputes_quote_expiry(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)

    updated = svc.update_shipment(
        db_session, created.id, ShipmentUpdate(quote_validity_days=7), ACTOR
    )

    assert updated.quote_expiry_date == created.quote_date + timedelta(days=7)  # type: ignore[operator]


def test_update_rejecting_merged_state_leaves_row_unchanged(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    assert created.eta is not None

    with pytest.raises(InvalidShipmentDataError):
        svc.update_shipment(
            db_session, created.id, ShipmentUpdate(etd=created.eta + timedelta(days=1)), ACTOR
        )

    assert svc.get_shipment(db_session, created.id).etd == created.etd
    assert [e.action for e in _events(db_session, created.id)] == ["shipment.created"]


def test_update_cannot_clear_a_required_field() -> None:
    with pytest.raises(ValidationError):
        ShipmentUpdate(cargo_type=None)


def test_noop_update_writes_no_audit_event(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)

    svc.update_shipment(
        db_session, created.id, ShipmentUpdate(cargo_type=created.cargo_type), ACTOR
    )

    assert len(_events(db_session, created.id)) == 1


# --------------------------------------------------------------------------- status transitions


def test_every_status_has_a_transition_entry() -> None:
    assert set(svc.TRANSITIONS) == set(ShipmentStatus)


def test_full_happy_path_lifecycle_is_allowed_and_each_move_audited(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    path = [S.BOOKED, S.IN_TRANSIT, S.DOCKED, S.UNDER_REVIEW, S.HELD, S.UNDER_REVIEW]
    path += [S.CLEARED, S.CLOSED]

    for status in path:
        if status is S.CLEARED:  # Phase 11: only by filing with an approval
            assert clear(db_session, created.id, ACTOR).status is status
        else:
            assert svc.update_status(db_session, created.id, status, ACTOR).status is status

    moves = [
        (e.payload["from"], e.payload["to"])
        for e in _events(db_session, created.id)
        if e.action == "shipment.status_changed"
    ]
    assert moves == list(zip(["quoted", *path[:-1]], path, strict=True))


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ([], S.DOCKED),  # quoted cannot skip booking/transit
        ([], S.QUOTED),  # no-op move to the current status
        ([S.BOOKED, S.IN_TRANSIT, S.DOCKED], S.CANCELLED),  # too late to cancel at port
        ([S.BOOKED, S.CANCELLED], S.BOOKED),  # cancelled is terminal
    ],
)
def test_illegal_transitions_raise(
    db_session: Session, path: list[ShipmentStatus], illegal: ShipmentStatus
) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    for status in path:
        svc.update_status(db_session, created.id, status, ACTOR)

    with pytest.raises(InvalidStatusTransitionError):
        svc.update_status(db_session, created.id, illegal, ACTOR)


def test_closed_is_terminal(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    for status in [S.BOOKED, S.IN_TRANSIT, S.DOCKED, S.UNDER_REVIEW]:
        svc.update_status(db_session, created.id, status, ACTOR)
    clear(db_session, created.id, ACTOR)
    svc.update_status(db_session, created.id, S.CLOSED, ACTOR)

    assert svc.allowed_transitions(S.CLOSED) == frozenset()
    with pytest.raises(InvalidStatusTransitionError):
        svc.update_status(db_session, created.id, S.HELD, ACTOR)


# --------------------------------------------------------------------------- clearance (Phase 11)


def _under_review(db_session: Session) -> ShipmentRecord:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    for status in [S.BOOKED, S.IN_TRANSIT, S.DOCKED, S.UNDER_REVIEW]:
        svc.update_status(db_session, created.id, status, ACTOR)
    return created


@pytest.mark.parametrize(
    "path", [[S.BOOKED, S.IN_TRANSIT, S.DOCKED], [S.BOOKED, S.IN_TRANSIT, S.DOCKED, S.UNDER_REVIEW]]
)
def test_update_status_never_clears(db_session: Session, path: list[ShipmentStatus]) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    for status in path:
        svc.update_status(db_session, created.id, status, ACTOR)

    with pytest.raises(ClearanceRequiresApprovalError):
        svc.update_status(db_session, created.id, S.CLEARED, ACTOR)
    assert svc.get_shipment(db_session, created.id).status is path[-1]


def test_clear_shipment_cites_the_approval_in_the_audit(db_session: Session) -> None:
    created = _under_review(db_session)
    decision = approve_filing(db_session, created.id)

    cleared = svc.clear_shipment(db_session, created.id, decision.id, ACTOR)

    assert cleared.status is S.CLEARED
    last = [e for e in _events(db_session, created.id) if e.action == "shipment.status_changed"][-1]
    assert last.payload["to"] == "cleared"
    assert last.payload["filing_decision_id"] == str(decision.id)
    assert last.payload["approved_by"] == decision.reviewer


def test_clear_shipment_needs_an_approval_of_this_shipment(db_session: Session) -> None:
    created = _under_review(db_session)
    other = svc.create_shipment(db_session, make_shipment(container_id="TSTU0000002"), ACTOR)
    elsewhere = approve_filing(db_session, other.id)

    with pytest.raises(FilingDecisionNotFoundError):
        svc.clear_shipment(db_session, created.id, uuid4(), ACTOR)
    with pytest.raises(InvalidFilingDecisionError, match="another shipment"):
        svc.clear_shipment(db_session, created.id, elsewhere.id, ACTOR)
    assert svc.get_shipment(db_session, created.id).status is S.UNDER_REVIEW


def test_clear_shipment_refuses_a_rejection(db_session: Session) -> None:
    created = _under_review(db_session)
    run_id = str(uuid4())
    governance_service.start_run(db_session, run_id, created.id, date(2026, 9, 28), ACTOR)
    governance_service.update_run(db_session, run_id, RunState.AWAITING_FILING, actor=ACTOR)
    rejection = governance_service.record_filing_decision(
        db_session, run_id, "reject", "Asha Rao", "valuation query", checklist={},
        ready_for_clearance=True,
    )  # fmt: skip

    with pytest.raises(InvalidFilingDecisionError, match="rejection"):
        svc.clear_shipment(db_session, created.id, rejection.id, ACTOR)


def test_clear_shipment_only_from_under_review(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    for status in [S.BOOKED, S.IN_TRANSIT, S.DOCKED]:
        svc.update_status(db_session, created.id, status, ACTOR)
    decision = approve_filing(db_session, created.id)

    with pytest.raises(InvalidStatusTransitionError, match="under_review"):
        svc.clear_shipment(db_session, created.id, decision.id, ACTOR)


def test_database_refuses_clearing_without_an_approval(db_session: Session) -> None:
    created = _under_review(db_session)
    with pytest.raises(IntegrityError, match="approved filing decision"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE shipping.shipments SET status = 'cleared' WHERE id = :id"),
            {"id": created.id},
        )


# --------------------------------------------------------------------------- cancellation


def test_cancelled_shipment_short_circuits_active_reads_and_edits(db_session: Session) -> None:
    """Edge case (Phase 1): a pipeline reading a shipment cancelled mid-run must stop with a
    typed error rather than complete its analysis."""
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)
    assert svc.get_active_shipment(db_session, created.id).id == created.id

    svc.update_status(db_session, created.id, S.CANCELLED, ACTOR)

    with pytest.raises(ShipmentCancelledError):
        svc.get_active_shipment(db_session, created.id)
    with pytest.raises(ShipmentCancelledError):
        svc.update_shipment(db_session, created.id, ShipmentUpdate(cargo_type="x"), ACTOR)
    with pytest.raises(ShipmentCancelledError):
        svc.add_document(db_session, created.id, _invoice(), ACTOR)
    # Nothing is deleted: the cancelled shipment is still readable for audit.
    assert svc.get_shipment(db_session, created.id).status is S.CANCELLED


# --------------------------------------------------------------------------- documents


def test_add_document_starts_a_version_1_chain(db_session: Session) -> None:
    shipment = svc.create_shipment(db_session, make_shipment(), ACTOR)

    doc = svc.add_document(db_session, shipment.id, _invoice(), ACTOR)

    assert (doc.version, doc.superseded_by, doc.shipment_id) == (1, None, shipment.id)
    assert svc.get_documents(db_session, shipment.id) == [doc]
    assert _events(db_session, doc.id)[0].action == "document.added"


def test_add_document_to_unknown_shipment_raises(db_session: Session) -> None:
    with pytest.raises(ShipmentNotFoundError):
        svc.add_document(db_session, uuid.uuid4(), _invoice(), ACTOR)


def test_amend_document_chains_versions_and_hides_superseded_by_default(
    db_session: Session,
) -> None:
    shipment = svc.create_shipment(db_session, make_shipment(), ACTOR)
    v1 = svc.add_document(db_session, shipment.id, _invoice(), ACTOR)

    v2 = svc.amend_document(db_session, v1.id, DocumentAmend(qty=Decimal("95")), ACTOR)

    assert (v2.version, v2.doc_type, v2.qty) == (2, DocType.INVOICE, Decimal("95"))
    assert (v2.value, v2.currency, v2.issued_date) == (v1.value, v1.currency, v1.issued_date)
    assert svc.get_documents(db_session, shipment.id) == [v2]
    history = svc.get_documents(db_session, shipment.id, include_superseded=True)
    assert [(d.version, d.superseded_by) for d in history] == [(1, v2.id), (2, None)]
    event = _events(db_session, v2.id)[0]
    assert (event.action, event.payload["supersedes"]) == ("document.amended", str(v1.id))


def test_only_latest_document_version_can_be_amended(db_session: Session) -> None:
    shipment = svc.create_shipment(db_session, make_shipment(), ACTOR)
    v1 = svc.add_document(db_session, shipment.id, _invoice(), ACTOR)
    svc.amend_document(db_session, v1.id, DocumentAmend(qty=Decimal("95")), ACTOR)

    with pytest.raises(DocumentSupersededError):
        svc.amend_document(db_session, v1.id, DocumentAmend(qty=Decimal("90")), ACTOR)


def test_amend_unknown_document_raises(db_session: Session) -> None:
    with pytest.raises(DocumentNotFoundError):
        svc.amend_document(db_session, uuid.uuid4(), DocumentAmend(qty=Decimal("1")), ACTOR)


def test_conflicting_documents_of_the_same_type_are_both_kept(db_session: Session) -> None:
    """Edge case (Phase 1): two invoices with different qty are both returned, so the
    verification agent can report both values instead of silently preferring one."""
    shipment = svc.create_shipment(db_session, make_shipment(), ACTOR)
    svc.add_document(db_session, shipment.id, _invoice(qty=Decimal("100")), ACTOR)
    svc.add_document(db_session, shipment.id, _invoice(qty=Decimal("120")), ACTOR)

    qtys = sorted(d.qty for d in svc.get_documents(db_session, shipment.id) if d.qty is not None)

    assert qtys == [Decimal("100"), Decimal("120")]


def test_eta_can_be_unknown(db_session: Session) -> None:
    """Edge case (Phase 1): eta=None is a valid, persisted state (the demurrage agent later
    reports it as indeterminate risk rather than assuming zero)."""
    created = svc.create_shipment(db_session, make_shipment(eta=None), ACTOR)

    assert svc.get_shipment(db_session, created.id).eta is None


def test_document_amend_cannot_clear_issued_date() -> None:
    with pytest.raises(ValidationError):
        DocumentAmend(issued_date=None)


def test_audit_rows_roll_back_with_the_change(db_session: Session) -> None:
    """A mutation and its audit event share a transaction: rolling back drops both."""
    before = db_session.scalar(select(func.count()).select_from(AuditEvent))
    nested = db_session.begin_nested()
    svc.create_shipment(db_session, make_shipment(container_id="RLBU0000001"), ACTOR)
    nested.rollback()

    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == before
    with pytest.raises(ShipmentNotFoundError):
        svc.get_shipment_by_container_id(db_session, "RLBU0000001")


# --------------------------------------------------------------------------- container type


def test_container_type_is_stored_and_editable_with_audit(db_session: Session) -> None:
    created = svc.create_shipment(
        db_session, make_shipment(container_type=ContainerType.HIGH_CUBE_40), ACTOR
    )
    assert created.container_type is ContainerType.HIGH_CUBE_40

    updated = svc.update_shipment(
        db_session, created.id, ShipmentUpdate(container_type=ContainerType.DRY_20), ACTOR
    )

    assert updated.container_type is ContainerType.DRY_20
    event = _events(db_session, created.id)[-1]
    assert event.action == "shipment.updated"
    assert event.payload["changes"]["container_type"] == {"from": "40hc", "to": "20ft"}


@pytest.mark.parametrize("container_type", [None, "45ft"])
def test_container_type_is_required_and_must_be_a_known_type(container_type: str | None) -> None:
    """Demurrage is tariffed per container type, so it is never left to an assumption."""
    fields = make_shipment().model_dump(exclude={"container_type"})
    if container_type is not None:
        fields["container_type"] = container_type
    with pytest.raises(ValidationError, match="container_type"):
        ShipmentCreate(**fields)


def test_container_type_cannot_be_cleared() -> None:
    with pytest.raises(ValidationError, match="container_type"):
        ShipmentUpdate(container_type=None)


def test_database_rejects_an_unknown_container_type(db_session: Session) -> None:
    created = svc.create_shipment(db_session, make_shipment(), ACTOR)

    bypassing_the_service = text(
        "UPDATE shipping.shipments SET container_type = '45ft' WHERE id = :id"
    )
    with (
        pytest.raises(IntegrityError, match="ck_shipments_known_container_type"),
        db_session.begin_nested(),
    ):
        db_session.execute(bypassing_the_service, {"id": created.id})

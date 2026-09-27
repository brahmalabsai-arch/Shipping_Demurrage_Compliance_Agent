"""Phase 2 — demurrage_service: historical lookups, tariff revisions, charge maths, edge cases."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import AuditEvent, PortRate
from models.enums import ContainerType
from models.schemas import PortRateUpsert
from services import demurrage_service as svc
from services.errors import NegativeStayError, OverlappingRateError, PortRateNotFoundError

ACTOR = "test-operator"
C = ContainerType


def _upsert(**overrides: Any) -> PortRateUpsert:
    fields: dict[str, Any] = {
        "port_code": "INNSA",
        "container_type": C.DRY_20,
        "currency": "USD",
        "free_days": 5,
        "daily_rate": Decimal("45.00"),
        "effective_from": date(2026, 1, 1),
    }
    fields.update(overrides)
    return PortRateUpsert.model_validate(fields)


def _actions(session: Session) -> list[str]:
    query = select(AuditEvent.action).where(AuditEvent.entity_type == "port_rate")
    return list(session.scalars(query.order_by(AuditEvent.created_at)))


# --------------------------------------------------------------------------- lookups


def test_unconfigured_port_raises_typed_error(db_session: Session) -> None:
    """Edge case: get_rate("UNKNOWN", "20ft", date) → PortRateNotFoundError (not a falsy value)."""
    with pytest.raises(PortRateNotFoundError):
        svc.get_rate(db_session, "UNKNOWN", "20ft", date(2026, 9, 8))


def test_configured_port_but_other_container_type_raises(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(), ACTOR)

    with pytest.raises(PortRateNotFoundError):
        svc.get_rate(db_session, "INNSA", C.DRY_40, date(2026, 9, 8))


def test_date_before_first_period_raises_rather_than_using_nearest(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(effective_from=date(2026, 1, 1)), ACTOR)

    with pytest.raises(PortRateNotFoundError):
        svc.get_rate(db_session, "INNSA", C.DRY_20, date(2025, 12, 31))


def test_future_dated_revision_does_not_change_historical_lookup(db_session: Session) -> None:
    """Edge case: a revision effective in the future must not affect a shipment that docked
    under the current rate — get_rate(as_of=dock_date) returns the rate active on that date."""
    current = svc.upsert_rate(db_session, _upsert(daily_rate=Decimal("45.00")), ACTOR)
    dock_date = date(2026, 9, 8)

    revision = svc.upsert_rate(
        db_session, _upsert(daily_rate=Decimal("55.00"), effective_from=date(2026, 10, 1)), ACTOR
    )

    on_dock = svc.get_rate(db_session, "INNSA", C.DRY_20, dock_date)
    assert (on_dock.id, on_dock.daily_rate) == (current.id, Decimal("45.00"))
    after = svc.get_rate(db_session, "INNSA", C.DRY_20, date(2026, 10, 1))
    assert (after.id, after.daily_rate) == (revision.id, Decimal("55.00"))


def test_period_boundaries_are_inclusive(db_session: Session) -> None:
    first = svc.upsert_rate(db_session, _upsert(), ACTOR)
    second = svc.upsert_rate(db_session, _upsert(effective_from=date(2026, 10, 1)), ACTOR)

    assert svc.get_rate(db_session, "INNSA", C.DRY_20, date(2026, 9, 30)).id == first.id
    assert svc.get_rate(db_session, "INNSA", C.DRY_20, date(2026, 10, 1)).id == second.id


# --------------------------------------------------------------------------- upsert semantics


def test_new_period_closes_the_previous_one_and_audits_it(db_session: Session) -> None:
    first = svc.upsert_rate(db_session, _upsert(), ACTOR)
    assert first.effective_to is None

    svc.upsert_rate(db_session, _upsert(effective_from=date(2026, 10, 1)), ACTOR)

    closed = svc.get_rate(db_session, "INNSA", C.DRY_20, date(2026, 5, 1))
    assert closed.effective_to == date(2026, 9, 30)
    assert _actions(db_session) == [
        "port_rate.created",
        "port_rate.superseded",
        "port_rate.created",
    ]
    assert len(svc.list_rates(db_session)) == 2  # history kept, nothing overwritten


def test_backdated_period_is_bounded_by_the_next_one(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(effective_from=date(2026, 1, 1)), ACTOR)
    svc.upsert_rate(db_session, _upsert(effective_from=date(2026, 10, 1)), ACTOR)

    middle = svc.upsert_rate(
        db_session, _upsert(effective_from=date(2026, 6, 1), daily_rate=Decimal("50")), ACTOR
    )

    assert middle.effective_to == date(2026, 9, 30)
    periods = [(r.effective_from, r.effective_to) for r in svc.list_rates(db_session)]
    assert periods == [
        (date(2026, 1, 1), date(2026, 5, 31)),
        (date(2026, 6, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), None),
    ]


def test_same_start_day_corrects_in_place_and_audits_the_diff(db_session: Session) -> None:
    original = svc.upsert_rate(db_session, _upsert(), ACTOR)

    corrected = svc.upsert_rate(db_session, _upsert(daily_rate=Decimal("47.50")), ACTOR)

    assert corrected.id == original.id
    assert corrected.daily_rate == Decimal("47.50")
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "port_rate.updated")
    ).one()
    assert event.payload == {"changes": {"daily_rate": {"from": "45.00", "to": "47.50"}}}


def test_identical_upsert_is_a_no_op(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(), ACTOR)

    svc.upsert_rate(db_session, _upsert(), ACTOR)

    assert _actions(db_session) == ["port_rate.created"]


def test_explicit_end_overlapping_next_period_raises(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(effective_from=date(2026, 10, 1)), ACTOR)

    with pytest.raises(OverlappingRateError):
        svc.upsert_rate(
            db_session,
            _upsert(effective_from=date(2026, 1, 1), effective_to=date(2026, 10, 15)),
            ACTOR,
        )


def test_container_types_have_independent_periods(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(container_type=C.DRY_20), ACTOR)
    svc.upsert_rate(db_session, _upsert(container_type=C.DRY_40, daily_rate=Decimal("90")), ACTOR)

    svc.upsert_rate(
        db_session, _upsert(container_type=C.DRY_20, effective_from=date(2026, 7, 1)), ACTOR
    )

    forty = svc.get_rate(db_session, "INNSA", C.DRY_40, date(2026, 12, 1))
    assert (forty.daily_rate, forty.effective_to) == (Decimal("90"), None)


def test_database_rejects_overlapping_periods_even_bypassing_the_service(
    db_session: Session,
) -> None:
    """The no-overlap guarantee lives in Postgres (GiST exclusion), not only in upsert_rate."""
    svc.upsert_rate(db_session, _upsert(), ACTOR)

    with pytest.raises(IntegrityError, match="ex_port_rates_no_overlap"), db_session.begin_nested():
        db_session.add(
            PortRate(
                port_code="INNSA",
                container_type="20ft",
                currency="USD",
                free_days=5,
                daily_rate=Decimal("1"),
                effective_from=date(2026, 3, 1),
                effective_to=None,
            )
        )
        db_session.flush()


def test_database_rejects_negative_values_even_bypassing_the_service(db_session: Session) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_port_rates_non_negative"),
        db_session.begin_nested(),
    ):
        db_session.execute(
            text(
                "INSERT INTO demurrage.port_rates (port_code, container_type, currency, free_days,"
                " daily_rate, effective_from) VALUES ('INNSA', '20ft', 'USD', -1, 10, '2026-01-01')"
            )
        )


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"daily_rate": Decimal("-0.01")},
        {"free_days": -1},
        {"container_type": "30ft"},
        {"port_code": "jnpt"},
        {"effective_to": date(2025, 12, 31)},  # before effective_from
    ],
)
def test_invalid_tariffs_are_rejected_by_pydantic_before_the_service(
    overrides: dict[str, Any],
) -> None:
    """Edge case: negative daily_rate/free_days never reach upsert_rate (ge=0 constraints)."""
    with pytest.raises(ValidationError):
        _upsert(**overrides)


# --------------------------------------------------------------------------- charge maths


def test_free_days_zero_is_accepted_and_charges_from_day_one(db_session: Session) -> None:
    """Edge case: free_days=0 is a valid tariff; charging then starts on day 1."""
    rate = svc.upsert_rate(db_session, _upsert(free_days=0, daily_rate=Decimal("45.00")), ACTOR)

    charge = svc.demurrage_charge(rate, 1)

    assert (rate.free_days, charge.chargeable_days, charge.amount) == (0, 1, Decimal("45.00"))


@pytest.mark.parametrize(
    ("days", "chargeable", "amount"),
    [(0, 0, "0.00"), (5, 0, "0.00"), (6, 1, "45.00"), (10, 5, "225.00")],
)
def test_charge_is_days_beyond_free_allowance_times_daily_rate(
    db_session: Session, days: int, chargeable: int, amount: str
) -> None:
    rate = svc.upsert_rate(db_session, _upsert(free_days=5, daily_rate=Decimal("45.00")), ACTOR)

    charge = svc.demurrage_charge(rate, days)

    assert (charge.chargeable_days, charge.amount, charge.currency) == (
        chargeable,
        Decimal(amount),
        "USD",
    )


def test_negative_stay_raises_typed_error(db_session: Session) -> None:
    rate = svc.upsert_rate(db_session, _upsert(), ACTOR)

    with pytest.raises(NegativeStayError):
        svc.demurrage_charge(rate, -1)


def test_list_ports(db_session: Session) -> None:
    svc.upsert_rate(db_session, _upsert(port_code="INNSA"), ACTOR)
    svc.upsert_rate(db_session, _upsert(port_code="INMUN"), ACTOR)

    assert svc.list_ports(db_session) == ["INMUN", "INNSA"]

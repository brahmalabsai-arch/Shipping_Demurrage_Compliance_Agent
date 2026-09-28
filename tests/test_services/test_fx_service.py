"""Phase 3 — fx_service: the four-stage resolution engine, rate-table writes, edge cases."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from models.db_models import AuditEvent, GazettedRate
from models.enums import FxStage, InvoiceKind, LockType, RateSource
from models.schemas import GazettedRateCreate, LockedRateCreate, SpotRateUpsert
from scripts import seed_fx_rates
from services import fx_service as svc
from services.errors import (
    GazettedRateNotFoundError,
    InvalidGazettedRevisionError,
    InvalidRateRequestError,
    OverlappingGazettedPeriodError,
    RateAlreadyLockedError,
    RateNotFoundError,
)
from tests.fixtures.fx_rates import EUR, JPY, USD

ACTOR = "test-operator"
S, K = FxStage, InvoiceKind


def _d(month: int, day: int) -> date:
    return date(2026, month, day)


def _spot(session: Session, rate: str, as_of: datetime, pair: str = USD) -> Any:
    data = SpotRateUpsert(pair=pair, rate=Decimal(rate), as_of=as_of, source="test-feed")
    return svc.upsert_spot_rate(session, data, ACTOR)


def _lock(session: Session, rate: str, day: date, lock_type: LockType, pair: str = USD) -> Any:
    data = LockedRateCreate(pair=pair, rate=Decimal(rate), lock_date=day, lock_type=lock_type)
    return svc.lock_rate(session, data, ACTOR)


def _gazette(session: Session, rate: str, start: date, end: date, **kw: Any) -> Any:
    data = GazettedRateCreate(
        pair=kw.pop("pair", USD),
        rate=Decimal(rate),
        period_start=start,
        period_end=end,
        publishing_authority="Test Customs",
        **kw,
    )
    return svc.publish_gazetted_rate(session, data, ACTOR)


def _actions(session: Session, entity_type: str) -> list[str]:
    query = select(AuditEvent.action).where(AuditEvent.entity_type == entity_type)
    return list(session.scalars(query.order_by(AuditEvent.created_at)))


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    seed_fx_rates.seed()


# --------------------------------------------------------------------------- four stages


@pytest.mark.parametrize(
    ("pair", "as_of", "stage", "context", "rate", "source"),
    [
        # quotation: latest spot on/before the quote date
        pytest.param(USD, _d(8, 10), S.QUOTATION, {"quote_validity_days": 14}, "87.6150",
                     RateSource.SPOT, id="quotation-clean"),
        pytest.param(EUR, _d(7, 20), S.QUOTATION, {"quote_validity_days": 14}, "101.8500",
                     RateSource.SPOT, id="quotation-delayed"),
        pytest.param(USD, _d(8, 11), S.QUOTATION, {"quote_validity_days": 10}, "87.6150",
                     RateSource.SPOT, id="quotation-day-without-spot-uses-previous"),
        # bol: exact lock on the on-board date
        pytest.param(USD, _d(8, 22), S.BOL, {}, "87.8600", RateSource.LOCKED, id="bol-clean"),
        pytest.param(EUR, _d(8, 1), S.BOL, {}, "102.0500", RateSource.LOCKED, id="bol-delayed"),
        pytest.param(JPY, _d(9, 10), S.BOL, {}, "0.6004", RateSource.LOCKED,
                     id="bol-missing-document"),
        # invoice: commercial locks to its own date; freight re-uses the BoL lock
        pytest.param(USD, _d(8, 18), S.INVOICE, {"invoice_kind": K.COMMERCIAL}, "87.7050",
                     RateSource.LOCKED, id="invoice-commercial-clean"),
        pytest.param(EUR, _d(7, 28), S.INVOICE, {"invoice_kind": K.COMMERCIAL}, "101.9700",
                     RateSource.LOCKED, id="invoice-commercial-delayed"),
        pytest.param(USD, _d(8, 29), S.INVOICE,
                     {"invoice_kind": K.FREIGHT, "bol_date": _d(8, 22)}, "87.8600",
                     RateSource.LOCKED, id="invoice-freight-clean"),
        # port_charges: gazetted period containing the date, revision preferred
        pytest.param(USD, _d(9, 20), S.PORT_CHARGES, {}, "88.30", RateSource.GAZETTED,
                     id="port-charges-clean-original"),
        pytest.param(USD, _d(9, 22), S.PORT_CHARGES, {}, "88.40", RateSource.GAZETTED,
                     id="port-charges-qty-mismatch-mid-period-revision"),
        pytest.param(EUR, _d(9, 8), S.PORT_CHARGES, {}, "103.05", RateSource.GAZETTED,
                     id="port-charges-delayed-full-period-revision"),
        pytest.param(JPY, _d(10, 5), S.PORT_CHARGES, {}, "0.6030", RateSource.GAZETTED,
                     id="port-charges-missing-document"),
    ],
)  # fmt: skip
def test_each_stage_resolves_the_fixture_rate(
    seeded: None,
    db_session: Session,
    pair: str,
    as_of: date,
    stage: FxStage,
    context: dict[str, Any],
    rate: str,
    source: RateSource,
) -> None:
    resolved = svc.resolve_rate(db_session, pair, as_of, stage, **context)

    assert (resolved.pair, resolved.stage, resolved.as_of) == (pair, stage, as_of)
    assert (resolved.rate, resolved.source) == (Decimal(rate), source)
    assert resolved.locked is (source is RateSource.LOCKED)


def test_bol_date_resolves_to_its_exact_locked_rate_not_spot(db_session: Session) -> None:
    """Exit criterion: a BoL date resolves to its locked rate even with a spot rate that day."""
    locked = _lock(db_session, "87.8600", _d(8, 22), LockType.BOL)
    _spot(db_session, "87.9999", datetime(2026, 8, 22, 6, tzinfo=UTC))

    resolved = svc.resolve_rate(db_session, USD, _d(8, 22), S.BOL)

    assert (resolved.rate, resolved.source_id) == (Decimal("87.8600"), locked.id)


def test_port_charge_date_resolves_to_gazetted_period_even_when_spot_differs(
    db_session: Session,
) -> None:
    """Exit criterion: port charges use the gazetted period rate, not that day's spot."""
    gazetted = _gazette(db_session, "88.30", _d(9, 16), _d(9, 30))
    _spot(db_session, "88.3550", datetime(2026, 9, 20, 8, tzinfo=UTC))

    resolved = svc.resolve_rate(db_session, USD, _d(9, 20), S.PORT_CHARGES)

    assert (resolved.rate, resolved.source_id) == (Decimal("88.30"), gazetted.id)
    assert svc.get_spot_rate(db_session, USD, _d(9, 20)).rate == Decimal("88.3550")


# --------------------------------------------------------------------------- edge cases


def test_saturday_without_locked_row_raises_rather_than_nearest_date(db_session: Session) -> None:
    """Edge case: a Saturday with no locked_rates row → RateNotFoundError, even though locks
    exist on the Friday before and the Monday after."""
    saturday = _d(8, 29)
    assert saturday.strftime("%A") == "Saturday"
    _lock(db_session, "87.90", _d(8, 28), LockType.BOL)
    _lock(db_session, "87.95", _d(8, 31), LockType.BOL)

    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, USD, saturday, S.BOL)


def test_freight_and_commercial_invoices_straddling_a_rate_change_differ(
    db_session: Session,
) -> None:
    """Edge case: invoice_date and bol_date straddle a rate change — the freight invoice gets
    the BoL-locked rate, the commercial invoice the rate locked on its own date."""
    invoice_date, bol_date = _d(8, 20), _d(8, 25)
    _lock(db_session, "87.7800", invoice_date, LockType.COMMERCIAL_INVOICE)
    _lock(db_session, "87.9400", bol_date, LockType.BOL)

    commercial = svc.resolve_rate(
        db_session, USD, invoice_date, S.INVOICE, invoice_kind=K.COMMERCIAL
    )
    freight = svc.resolve_rate(
        db_session, USD, invoice_date, S.INVOICE, invoice_kind=K.FREIGHT, bol_date=bol_date
    )

    assert commercial.rate == Decimal("87.7800")
    assert freight.rate == Decimal("87.9400")


def test_commercial_invoice_ignores_a_bol_lock_on_the_same_day(db_session: Session) -> None:
    _lock(db_session, "87.8600", _d(8, 22), LockType.BOL)

    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, USD, _d(8, 22), S.INVOICE, invoice_kind=K.COMMERCIAL)


def test_overlapping_revision_is_preferred_over_the_original(db_session: Session) -> None:
    """Edge case: two rows with overlapping periods, one revision_of the other → the revision."""
    original = _gazette(db_session, "102.95", _d(9, 1), _d(9, 15), pair=EUR)
    revision = _gazette(
        db_session, "103.05", _d(9, 1), _d(9, 15), pair=EUR, revision_of=original.id
    )

    resolved = svc.resolve_rate(db_session, EUR, _d(9, 8), S.PORT_CHARGES)

    assert (resolved.rate, resolved.source_id) == (revision.rate, revision.id)
    assert resolved.rate != original.rate


def test_mid_period_revision_applies_only_from_its_own_start(db_session: Session) -> None:
    original = _gazette(db_session, "88.30", _d(9, 16), _d(9, 30))
    _gazette(db_session, "88.40", _d(9, 21), _d(9, 30), revision_of=original.id)

    before = svc.resolve_rate(db_session, USD, _d(9, 20), S.PORT_CHARGES)
    after = svc.resolve_rate(db_session, USD, _d(9, 21), S.PORT_CHARGES)

    assert (before.rate, after.rate) == (Decimal("88.30"), Decimal("88.40"))


def test_revision_of_a_revision_wins(db_session: Session) -> None:
    original = _gazette(db_session, "88.30", _d(9, 16), _d(9, 30))
    first = _gazette(db_session, "88.40", _d(9, 16), _d(9, 30), revision_of=original.id)
    second = _gazette(db_session, "88.35", _d(9, 25), _d(9, 30), revision_of=first.id)

    assert svc.get_gazetted_rate(db_session, USD, _d(9, 20)).id == first.id
    assert svc.get_gazetted_rate(db_session, USD, _d(9, 26)).id == second.id


def test_date_before_earliest_locked_row_raises_not_earliest_available(
    db_session: Session,
) -> None:
    """Edge case: a date before the earliest locked row → RateNotFoundError, not that row."""
    _lock(db_session, "101.9700", _d(7, 28), LockType.COMMERCIAL_INVOICE, pair=EUR)
    _lock(db_session, "102.0500", _d(8, 1), LockType.BOL, pair=EUR)

    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, EUR, _d(7, 27), S.INVOICE, invoice_kind=K.COMMERCIAL)
    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, EUR, _d(7, 31), S.BOL)


def test_quote_looked_up_ten_days_later_has_expired(seeded: None, db_session: Session) -> None:
    """Edge case: 7-day validity, checked 10 days later → ``expires`` is in the past, and the
    caller-side check (``is_quote_expired``, used by the dashboard and agents) flags it."""
    quote_date = _d(9, 1)
    checked_on = _d(9, 11)

    resolved = svc.resolve_rate(db_session, JPY, quote_date, S.QUOTATION, quote_validity_days=7)

    assert resolved.expires == _d(9, 8)
    assert resolved.expires < checked_on
    assert svc.is_quote_expired(resolved, checked_on) is True


def test_quote_is_valid_through_its_expiry_day(seeded: None, db_session: Session) -> None:
    resolved = svc.resolve_rate(db_session, JPY, _d(9, 1), S.QUOTATION, quote_validity_days=7)

    assert svc.is_quote_expired(resolved, _d(9, 8)) is False
    assert svc.is_quote_expired(resolved, _d(9, 9)) is True


def test_expiry_check_on_a_non_quotation_rate_raises(seeded: None, db_session: Session) -> None:
    resolved = svc.resolve_rate(db_session, USD, _d(8, 22), S.BOL)

    with pytest.raises(InvalidRateRequestError):
        svc.is_quote_expired(resolved, _d(9, 1))


def test_quotation_without_any_earlier_spot_raises(db_session: Session) -> None:
    _spot(db_session, "87.60", datetime(2026, 8, 10, 8, tzinfo=UTC))

    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, USD, _d(8, 9), S.QUOTATION, quote_validity_days=7)


def test_quote_day_is_the_ist_calendar_day(db_session: Session) -> None:
    """Spot at 23:00 IST on the quote day counts; 00:30 IST the next day does not."""
    late_same_day = _spot(db_session, "87.61", datetime(2026, 8, 10, 17, 30, tzinfo=UTC))
    _spot(db_session, "87.99", datetime(2026, 8, 10, 19, 0, tzinfo=UTC))  # 08-11 00:30 IST

    resolved = svc.resolve_rate(db_session, USD, _d(8, 10), S.QUOTATION, quote_validity_days=7)

    assert resolved.source_id == late_same_day.id


def test_gap_between_gazetted_periods_raises(db_session: Session) -> None:
    _gazette(db_session, "88.05", _d(9, 1), _d(9, 15))
    _gazette(db_session, "88.55", _d(10, 1), _d(10, 15))

    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, USD, _d(9, 20), S.PORT_CHARGES)


def test_unknown_pair_raises(seeded: None, db_session: Session) -> None:
    with pytest.raises(RateNotFoundError):
        svc.resolve_rate(db_session, "GBP/INR", _d(9, 8), S.PORT_CHARGES)


@pytest.mark.parametrize(
    ("stage", "context"),
    [
        (S.QUOTATION, {}),
        (S.QUOTATION, {"quote_validity_days": 6}),
        (S.QUOTATION, {"quote_validity_days": 15}),
        (S.QUOTATION, {"quote_validity_days": 7, "bol_date": _d(8, 22)}),
        (S.BOL, {"quote_validity_days": 7}),
        (S.BOL, {"invoice_kind": K.FREIGHT, "bol_date": _d(8, 22)}),
        (S.INVOICE, {}),
        (S.INVOICE, {"invoice_kind": K.FREIGHT}),
        (S.INVOICE, {"invoice_kind": K.COMMERCIAL, "bol_date": _d(8, 22)}),
        (S.PORT_CHARGES, {"bol_date": _d(8, 22)}),
    ],
)
def test_missing_or_irrelevant_stage_context_raises(
    db_session: Session, stage: FxStage, context: dict[str, Any]
) -> None:
    with pytest.raises(InvalidRateRequestError):
        svc.resolve_rate(db_session, USD, _d(8, 22), stage, **context)


# --------------------------------------------------------------------------- spot writes


def test_spot_correction_edits_in_place_and_audits_the_diff(db_session: Session) -> None:
    at = datetime(2026, 8, 10, 8, tzinfo=UTC)
    original = _spot(db_session, "87.6150", at)

    corrected = _spot(db_session, "87.6200", at)

    assert corrected.id == original.id
    assert corrected.rate == Decimal("87.6200")
    assert _actions(db_session, "spot_rate") == ["spot_rate.created", "spot_rate.updated"]
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "spot_rate.updated")
    ).one()
    assert event.payload == {"changes": {"rate": {"from": "87.615000", "to": "87.6200"}}}


def test_identical_spot_re_entry_is_a_no_op(db_session: Session) -> None:
    at = datetime(2026, 8, 10, 8, tzinfo=UTC)
    _spot(db_session, "87.6150", at)

    _spot(db_session, "87.6150", at)

    assert _actions(db_session, "spot_rate") == ["spot_rate.created"]


def test_write_results_are_byte_identical_to_later_reads(db_session: Session) -> None:
    """Writes return what Postgres stored (NUMERIC(12,6) scale), not the input's own scale —
    so an MCP write result and a later read serialise identically (Phase 5 parity)."""
    at = datetime(2026, 10, 6, 8, tzinfo=UTC)
    spot = _spot(db_session, "88.5", at)
    locked = _lock(db_session, "88.44", _d(10, 6), LockType.BOL)
    gazetted = _gazette(db_session, "88.6", _d(10, 1), _d(10, 15))

    assert spot.model_dump_json() == svc.get_spot_rate(db_session, USD, _d(10, 6)).model_dump_json()
    assert spot.rate.as_tuple().exponent == -6
    assert (
        locked.model_dump_json()
        == svc.get_locked_rate(db_session, USD, LockType.BOL, _d(10, 6)).model_dump_json()
    )
    assert (
        gazetted.model_dump_json()
        == svc.get_gazetted_rate(db_session, USD, _d(10, 5)).model_dump_json()
    )


# --------------------------------------------------------------------------- locked writes


def test_locking_the_same_pair_type_and_date_twice_raises(db_session: Session) -> None:
    _lock(db_session, "87.8600", _d(8, 22), LockType.BOL)

    with pytest.raises(RateAlreadyLockedError, match=r"87\.86"):
        _lock(db_session, "90.0000", _d(8, 22), LockType.BOL)

    # the session stays usable and the original lock is untouched
    assert svc.get_locked_rate(db_session, USD, LockType.BOL, _d(8, 22)).rate == Decimal("87.86")
    assert _actions(db_session, "locked_rate") == ["locked_rate.created"]


def test_bol_and_commercial_invoice_can_lock_the_same_day(db_session: Session) -> None:
    _lock(db_session, "87.86", _d(8, 22), LockType.BOL)
    _lock(db_session, "87.80", _d(8, 22), LockType.COMMERCIAL_INVOICE)

    assert len(svc.list_locked_rates(db_session, USD)) == 2


def test_database_rejects_changing_a_locked_rate_even_bypassing_the_service(
    db_session: Session,
) -> None:
    """Immutability lives in Postgres (trigger), not only in the absence of an update API."""
    locked = _lock(db_session, "87.8600", _d(8, 22), LockType.BOL)

    for statement in (
        "UPDATE fx.locked_rates SET rate = 99 WHERE id = :id",
        "DELETE FROM fx.locked_rates WHERE id = :id",
    ):
        with pytest.raises(IntegrityError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(statement), {"id": locked.id})


# --------------------------------------------------------------------------- gazetted writes


def test_overlapping_original_notifications_raise(db_session: Session) -> None:
    _gazette(db_session, "88.05", _d(9, 1), _d(9, 15))

    with pytest.raises(OverlappingGazettedPeriodError):
        _gazette(db_session, "88.10", _d(9, 15), _d(9, 30))

    # other pairs are independent
    _gazette(db_session, "102.95", _d(9, 1), _d(9, 15), pair=EUR)


def test_sibling_revisions_may_not_overlap(db_session: Session) -> None:
    original = _gazette(db_session, "88.30", _d(9, 16), _d(9, 30))
    _gazette(db_session, "88.40", _d(9, 21), _d(9, 30), revision_of=original.id)

    with pytest.raises(OverlappingGazettedPeriodError, match="revise that revision"):
        _gazette(db_session, "88.45", _d(9, 25), _d(9, 30), revision_of=original.id)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"period_start": _d(8, 31)}, InvalidGazettedRevisionError),  # starts before parent
        ({"period_end": _d(10, 1)}, InvalidGazettedRevisionError),  # ends after parent
        ({"pair": EUR}, InvalidGazettedRevisionError),
        ({"revision_of": uuid4()}, GazettedRateNotFoundError),
    ],
)
def test_invalid_revisions_raise(
    db_session: Session, overrides: dict[str, Any], error: type[Exception]
) -> None:
    original = _gazette(db_session, "88.05", _d(9, 1), _d(9, 15))
    kw: dict[str, Any] = {
        "period_start": _d(9, 1),
        "period_end": _d(9, 15),
        "revision_of": original.id,
    }
    kw.update(overrides)

    with pytest.raises(error):
        _gazette(db_session, "88.10", kw.pop("period_start"), kw.pop("period_end"), **kw)


def test_gazetted_writes_are_audited(db_session: Session) -> None:
    original = _gazette(db_session, "88.05", _d(9, 1), _d(9, 15))
    _gazette(db_session, "88.10", _d(9, 1), _d(9, 15), revision_of=original.id)

    assert _actions(db_session, "gazetted_rate") == [
        "gazetted_rate.created",
        "gazetted_rate.revised",
    ]


def test_database_rejects_overlapping_originals_and_edits_even_bypassing_the_service(
    db_session: Session,
) -> None:
    original = _gazette(db_session, "88.05", _d(9, 1), _d(9, 15))

    with (
        pytest.raises(IntegrityError, match="ex_gazetted_rates_no_overlap"),
        db_session.begin_nested(),
    ):
        db_session.add(
            GazettedRate(
                pair=USD,
                rate=Decimal("1"),
                period_start=_d(9, 10),
                period_end=_d(9, 20),
                publishing_authority="rogue",
            )
        )
        db_session.flush()

    with pytest.raises(IntegrityError, match="immutable"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE fx.gazetted_rates SET rate = 99 WHERE id = :id"), {"id": original.id}
        )


def test_database_rejects_non_positive_rates(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="positive_rate"), db_session.begin_nested():
        db_session.execute(
            text(
                "INSERT INTO fx.spot_rates (pair, rate, as_of, source)"
                " VALUES ('USD/INR', 0, now(), 'x')"
            )
        )


# --------------------------------------------------------------------------- validation & reads


@pytest.mark.parametrize(
    "overrides",
    [
        {"pair": "USDINR"},
        {"pair": "usd/inr"},
        {"pair": "INR/INR"},
        {"rate": Decimal("0")},
        {"rate": Decimal("-87.5")},
        {"rate": Decimal("87.1234567")},  # more precision than NUMERIC(12,6)
        {"as_of": datetime(2026, 8, 10, 8)},  # naive timestamp
    ],
)
def test_invalid_spot_rates_are_rejected_by_pydantic(overrides: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "pair": USD,
        "rate": Decimal("87.5"),
        "as_of": datetime(2026, 8, 10, 8, tzinfo=UTC),
        "source": "feed",
    }
    fields.update(overrides)
    with pytest.raises(ValidationError):
        SpotRateUpsert.model_validate(fields)


def test_gazetted_period_end_before_start_is_rejected_by_pydantic() -> None:
    with pytest.raises(ValidationError, match="period_end"):
        GazettedRateCreate(
            pair=USD,
            rate=Decimal("88"),
            period_start=_d(9, 15),
            period_end=_d(9, 1),
            publishing_authority="x",
        )


def test_seeded_lists_and_pairs(seeded: None, db_session: Session) -> None:
    assert svc.list_pairs(db_session) == [EUR, JPY, USD]
    assert len(svc.list_spot_rates(db_session, USD)) == 8
    assert len(svc.list_locked_rates(db_session)) == 7
    usd_gazetted = svc.list_gazetted_rates(db_session, USD)
    assert len(usd_gazetted) == 9
    # the revision is listed straight after the original it revises
    sept = [r for r in usd_gazetted if r.period_start >= _d(9, 16) and r.period_end <= _d(9, 30)]
    assert [r.revision_of is None for r in sept] == [True, False]
    assert sept[1].revision_of == sept[0].id

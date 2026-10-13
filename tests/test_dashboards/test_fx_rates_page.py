"""Phase 3 — FX Rates dashboard: edits are immediately visible to ``resolve_rate`` (no caching),
immutability and revision rules surface as typed errors, and an expired quote is flagged by the
caller (edge case: the resolver returns ``expires``; the page must say "expired")."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT
from models.db_models import LockedRate
from models.enums import FxStage, InvoiceKind, LockType
from scripts import seed_fx_rates
from services import fx_service as svc
from tests.fixtures.fx_rates import EUR, JPY, USD

PAGE = str(PROJECT_ROOT / "dashboards" / "views" / "3_FX_Rates.py")


def _open_page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


def _resolve(at: AppTest, stage: FxStage, pair: str, as_of: date, **fields: object) -> AppTest:
    at.selectbox(key="resolve_stage").set_value(stage)
    at.run()
    at.selectbox(key="resolve_pair").set_value(pair)
    at.date_input(key="resolve_date").set_value(as_of)
    for key, value in fields.items():
        widget = {
            "resolve_validity": at.number_input,
            "resolve_check_on": at.date_input,
            "resolve_invoice_kind": at.selectbox,
            "resolve_bol_date": at.date_input,
        }[key](key=key)
        widget.set_value(value)
    at.button(key="resolve_submit").click()
    at.run()
    assert not at.exception, at.exception
    return at


def test_spot_rate_added_on_the_page_is_immediately_resolvable(
    session_factory: sessionmaker[Session],
) -> None:
    at = _open_page()
    at.text_input(key="spot_pair").set_value(USD)
    at.number_input(key="spot_rate").set_value(87.615)
    at.date_input(key="spot_date").set_value(date(2026, 8, 10))
    at.button(key="spot_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        resolved = svc.resolve_rate(
            session, USD, date(2026, 8, 12), FxStage.QUOTATION, quote_validity_days=10
        )
    assert resolved.rate == Decimal("87.615")

    _resolve(
        at,
        FxStage.QUOTATION,
        USD,
        date(2026, 8, 12),
        resolve_validity=10,
        resolve_check_on=date(2026, 8, 20),
    )
    assert "87.615" in at.success[0].value
    assert "valid until 22 Aug 2026" in at.info[0].value


def test_expired_quote_is_surfaced_by_the_page(session_factory: sessionmaker[Session]) -> None:
    """Edge case: 7-day validity checked 10 days later → the caller shows 'Quote expired'."""
    seed_fx_rates.seed()
    at = _open_page()

    _resolve(
        at,
        FxStage.QUOTATION,
        JPY,
        date(2026, 9, 1),
        resolve_validity=7,
        resolve_check_on=date(2026, 9, 11),
    )

    assert "0.5987" in at.success[0].value
    assert at.warning[0].value.startswith("The quote expired on 08 Sep 2026")


def test_freight_invoice_on_the_page_uses_the_bol_rate(
    session_factory: sessionmaker[Session],
) -> None:
    seed_fx_rates.seed()
    at = _open_page()

    _resolve(
        at,
        FxStage.INVOICE,
        USD,
        date(2026, 8, 20),
        resolve_invoice_kind=InvoiceKind.FREIGHT,
        resolve_bol_date=date(2026, 8, 25),
    )

    assert "**87.94**" in at.success[0].value
    assert "locked on bol date 2026-08-25" in at.success[0].value


def test_locking_an_already_locked_date_shows_typed_error(
    session_factory: sessionmaker[Session],
) -> None:
    seed_fx_rates.seed()
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(LockedRate))
    at = _open_page()
    at.text_input(key="locked_pair").set_value(USD)
    at.selectbox(key="locked_type").set_value(LockType.BOL)
    at.date_input(key="locked_date").set_value(date(2026, 8, 22))
    at.number_input(key="locked_rate").set_value(99.0)
    at.button(key="locked_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert " is already locked at " in at.error[0].value
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LockedRate)) == before


def test_revision_published_on_the_page_is_preferred_by_resolve(
    session_factory: sessionmaker[Session],
) -> None:
    seed_fx_rates.seed()
    with session_factory() as session:
        parent = svc.get_gazetted_rate(session, EUR, date(2026, 10, 5))
    at = _open_page()
    label = next(
        o for o in at.selectbox(key="gazetted_revises").options if "EUR/INR 2026-10-01" in o
    )
    at.selectbox(key="gazetted_revises").set_value(label)
    at.run()
    suffix = str(parent.id)
    at.number_input(key=f"gazetted_rate_{suffix}").set_value(103.8)
    at.date_input(key=f"gazetted_start_{suffix}").set_value(date(2026, 10, 5))
    at.button(key=f"gazetted_submit_{suffix}").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        before = svc.resolve_rate(session, EUR, date(2026, 10, 4), FxStage.PORT_CHARGES)
        after = svc.resolve_rate(session, EUR, date(2026, 10, 5), FxStage.PORT_CHARGES)
    assert before.rate == parent.rate
    assert after.rate == Decimal("103.8")
    assert f"revision of {parent.id}" in after.basis


def test_zero_rate_is_rejected_by_validation(session_factory: sessionmaker[Session]) -> None:
    at = _open_page()
    at.text_input(key="locked_pair").set_value(USD)
    at.number_input(key="locked_rate").set_value(0.0)
    at.button(key="locked_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert "rate: input should be greater than 0" in at.error[0].value.lower()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LockedRate)) == 0


def test_unresolvable_date_shows_rate_not_found(session_factory: sessionmaker[Session]) -> None:
    seed_fx_rates.seed()
    at = _open_page()

    _resolve(at, FxStage.BOL, USD, date(2026, 8, 29))  # a Saturday with no lock

    assert at.error[0].value.startswith("No USD/INR rate locked for bol")

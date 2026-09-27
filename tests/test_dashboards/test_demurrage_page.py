"""Phase 2 exit criterion — a Demurrage dashboard edit is visible to ``get_rate`` in the same
test run, without a restart (no caching layer between the page and the service)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT
from models.db_models import PortRate
from models.enums import ContainerType
from services import demurrage_service as svc

PAGE = str(PROJECT_ROOT / "dashboards" / "pages" / "2_Demurrage.py")


def _open_page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


def _fill_new_rate(at: AppTest, daily_rate: float) -> None:
    at.text_input(key="rate_port_new").set_value("INNSA")
    at.selectbox(key="rate_ctype_new").set_value(ContainerType.DRY_40)
    at.number_input(key="rate_free_new").set_value(4)
    at.number_input(key="rate_daily_new").set_value(daily_rate)
    at.date_input(key="rate_from_new").set_value(date(2026, 1, 1))


def test_dashboard_edit_is_immediately_visible_to_get_rate(
    session_factory: sessionmaker[Session],
) -> None:
    at = _open_page()
    _fill_new_rate(at, 95.5)
    at.button(key="rate_submit_new").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with session_factory() as session:
        rate = svc.get_rate(session, "INNSA", ContainerType.DRY_40, date(2026, 9, 8))
    assert (rate.free_days, rate.daily_rate) == (4, Decimal("95.5"))

    # The lookup section of the (still running) page sees it too.
    at.selectbox(key="lookup_port").set_value("INNSA")
    at.selectbox(key="lookup_ctype").set_value(ContainerType.DRY_40)
    at.date_input(key="lookup_date").set_value(date(2026, 9, 8))
    at.number_input(key="lookup_days").set_value(7)
    at.button(key="lookup_submit").click()
    at.run()
    assert "95.5" in at.success[0].value
    assert at.metric[0].value == "286.5 USD"  # (7 - 4) days x 95.5


def test_negative_daily_rate_in_form_is_rejected_before_the_service(
    session_factory: sessionmaker[Session],
) -> None:
    """Edge case: a negative rate typed into the form fails Pydantic validation; no row."""
    at = _open_page()
    _fill_new_rate(at, -10.0)
    at.button(key="rate_submit_new").click()
    at.run()

    assert not at.exception, at.exception
    assert "daily_rate" in at.error[0].value
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(PortRate)) == 0


def test_lookup_for_unconfigured_port_shows_typed_error(
    session_factory: sessionmaker[Session],
) -> None:
    at = _open_page()  # no tariffs at all
    at.button(key="lookup_submit").click()
    at.run()

    assert not at.exception, at.exception
    assert at.error[0].value.startswith("PortRateNotFoundError")

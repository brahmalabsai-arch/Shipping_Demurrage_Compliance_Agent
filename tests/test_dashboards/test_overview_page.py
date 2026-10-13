"""The Overview page (the dashboards' front door) and the shared theme's dark-mode toggle."""

from datetime import date

from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from config.settings import PROJECT_ROOT
from dashboards import theme
from models.schemas import RoiAssumptions
from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments
from services import roi_service

PAGE = str(PROJECT_ROOT / "dashboards" / "views" / "0_Overview.py")


def _open_page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


def _markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_an_empty_desk_says_so(session_factory: sessionmaker[Session]) -> None:
    at = _open_page()
    text = _markdown(at)
    assert "Nothing is waiting on anyone right now." in text
    assert "No snapshot yet." in text
    assert "0 shipment(s) to Nhava Sheva" in text


def test_the_seeded_desk_shows_the_route_waiting_work_and_the_saving(
    session_factory: sessionmaker[Session],
) -> None:
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
    with session_factory() as session, session.begin():
        roi_service.capture_snapshot(session, "fixtures", date(2026, 9, 28), RoiAssumptions(), "t")

    text = _markdown(_open_page())

    assert "4 shipment(s) to Nhava Sheva" in text
    assert "1 held by customs" in text  # HLXU7004004 is held: shown beside the route
    assert '<div class="cc-stop "><span class="">2</span><b>Docked</b></div>' in text
    assert "INR 82,212.00" in text  # the documented saving, from the latest snapshot
    assert "Nothing is waiting on anyone right now." in text  # no pending rule, no open run


def test_dark_mode_is_a_session_choice_that_restyles_the_page(
    session_factory: sessionmaker[Session],
) -> None:
    at = _open_page()
    assert at.session_state[theme._DARK_STATE] is False

    at.sidebar.toggle(key="cc_dark_toggle").set_value(True).run()

    assert not at.exception, at.exception
    assert at.session_state[theme._DARK_STATE] is True
    assert theme.DARK["ground"] in theme._css(theme.DARK)
    assert "color-scheme: dark" in theme._css(theme.DARK)


def test_the_theme_palettes_cover_the_same_tokens() -> None:
    assert set(theme.LIGHT) == set(theme.DARK)


def _tabs_app() -> None:
    import streamlit as st

    from dashboards import theme

    waiting = st.session_state.get("waiting", 2)
    _, other = theme.tabs([f"Waiting ({waiting})", "Other"], key="tabs")
    with other:
        if st.button("Decide one", key="decide"):
            st.session_state["waiting"] = waiting - 1
            st.rerun()


def test_tabs_stay_open_across_an_action_and_a_changed_count() -> None:
    at = AppTest.from_function(_tabs_app).run()
    assert at.session_state["tabs"] == "Waiting (2)"  # the first tab by default

    at.session_state["tabs"] = "Other"
    at.run()
    # A browser sends every widget's value with a click; the test harness does not send the
    # tabs', so it is given explicitly. The click's rerun changes the label "(2)" -> "(1)".
    at.session_state["tabs"] = "Other"
    at.button(key="decide").click().run()
    assert not at.exception, at.exception
    assert at.session_state["tabs"] == "Other"  # the action's rerun kept the open tab

    at.session_state["tabs"] = "Waiting (1)"  # back to the first tab (a click reruns the page)
    at.run()
    at.session_state["waiting"] = 0
    at.run()
    assert at.session_state["tabs"] == "Waiting (0)"  # the same tab, its count changed


def _four_tabs_app() -> None:
    from dashboards import theme

    theme.tabs(["Analysis", "Clearance", "Papers", "Details"], key="shipment_tabs")


def test_switching_tabs_never_changes_their_identity() -> None:
    """Streamlit keys tabs by labels and default; a default that followed each click made every
    other click land on a stale widget and open the previous tab instead."""
    at = AppTest.from_function(_four_tabs_app).run()
    identity = at.session_state["_shipment_tabs_identity"]

    for label in ("Clearance", "Papers", "Details", "Analysis", "Papers", "Clearance"):
        at.session_state["shipment_tabs"] = label
        at.run()
        assert not at.exception, at.exception
        assert at.session_state["shipment_tabs"] == label
        assert at.session_state["_shipment_tabs_identity"] == identity

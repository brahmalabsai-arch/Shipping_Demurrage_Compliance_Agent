"""Phase 4 exit criterion — edit the bulletin on the dashboard, and ``diff_since_last`` called
twice without an intervening capture returns ``None`` the second time. Also: a dashboard edit
never captures a snapshot (Option A)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from streamlit.testing.v1 import AppTest

from agents.mcp_clients import memory_connector
from config.settings import PROJECT_ROOT
from integrations import email
from models.db_models import BulletinSnapshot, ChangeLogEntry
from scheduler import watch_scheduler
from scripts import seed_compliance
from services import bulletin_service as svc
from services import compliance_service
from tests.fixtures.compliance import BASELINE_BULLETIN, edit_section

PAGE = str(PROJECT_ROOT / "dashboards" / "pages" / "5_Customs_Bulletin.py")
SCANNER = "watch-scheduler"


def _open_page() -> AppTest:
    at = AppTest.from_file(PAGE, default_timeout=60).run()
    assert not at.exception, at.exception
    return at


def _publish_on_page(at: AppTest, text: str) -> None:
    at.text_area[0].set_value(text)
    at.button(key="bulletin_publish").click()
    at.run()
    assert not at.exception, at.exception


def _snapshot_count(session_factory: sessionmaker[Session]) -> int | None:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(BulletinSnapshot))


def test_dashboard_edit_then_diff_twice_returns_none_the_second_time(
    session_factory: sessionmaker[Session],
) -> None:
    seed_compliance.seed()
    with session_factory() as session, session.begin():
        svc.capture_snapshot(session, SCANNER)  # the scan before the edit
    edited = edit_section(
        BASELINE_BULLETIN, "HSN-8708-duty", "15% of the assessable", "20% of the assessable"
    )

    at = _open_page()
    _publish_on_page(at, edited)

    assert not at.error, [e.value for e in at.error]
    assert _snapshot_count(session_factory) == 1  # the edit itself captured nothing
    with session_factory() as session, session.begin():
        svc.capture_snapshot(session, SCANNER)  # the next scheduled scan
        first = svc.diff_since_last(session, SCANNER)
        second = svc.diff_since_last(session, SCANNER)
    assert first is not None and first.is_substantive
    assert second is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ChangeLogEntry)) == 1

    # The page's scan view shows the detected change and its diff.
    at.run()
    assert any("+20% of the assessable" in c.value for c in at.code)


def test_first_publish_on_an_empty_system(session_factory: sessionmaker[Session]) -> None:
    at = _open_page()
    assert "Nothing published yet" in at.info[0].value

    _publish_on_page(at, BASELINE_BULLETIN)

    assert at.success[0].value.startswith("Bulletin published")
    with session_factory() as session:
        assert svc.get_publication(session).updated_by == "bulletin-editor"
    assert _snapshot_count(session_factory) == 0


def test_malformed_bulletin_is_rejected_with_typed_error(
    session_factory: sessionmaker[Session],
) -> None:
    seed_compliance.seed()
    at = _open_page()

    _publish_on_page(at, BASELINE_BULLETIN.replace("Effective: 2026-04-01\n", "", 1))

    assert at.error[0].value.startswith("InvalidBulletinError")
    with session_factory() as session:
        assert svc.get_publication(session).content == BASELINE_BULLETIN


class _RecordingSender:
    backend = "recording"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, to: str, subject: str, body: str) -> str:
        self.sent.append(to)
        return "<1@test>"


def test_run_scan_button_fires_the_watch_job(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edit on the dashboard, then fire the scheduler job from the page: a pending proposal
    and a review email result (the Phase 6 demo path)."""
    sender = _RecordingSender()
    monkeypatch.setattr(watch_scheduler, "stdio_connector", memory_connector)
    monkeypatch.setattr(email, "get_email_sender", lambda: sender)
    seed_compliance.seed()
    with session_factory() as session, session.begin():
        svc.capture_snapshot(session, SCANNER)
    at = _open_page()
    _publish_on_page(at, edit_section(
        BASELINE_BULLETIN, "HSN-8708-duty", "15% of the assessable", "20% of the assessable"))  # fmt: skip

    at.button(key="run_scan").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert at.success[0].value.startswith("Scan complete: 1 proposal(s) pending review")
    with session_factory() as session:
        [pending] = compliance_service.list_pending(session)
    assert pending.rule_key == "HSN-8708-duty"
    assert len(sender.sent) == 1


def test_scans_tab_compares_the_latest_scan_with_the_rulebook(
    session_factory: sessionmaker[Session],
) -> None:
    seed_compliance.seed()
    with session_factory() as session, session.begin():
        svc.publish(session, edit_section(
            BASELINE_BULLETIN, "HSN-8708-duty", "15% of the assessable", "20% of the assessable"),
            "editor")  # fmt: skip
        svc.capture_snapshot(session, SCANNER)

    at = _open_page()

    drift = at.dataframe[1].value
    assert list(drift["Rule"]) == ["HSN-8708-duty"]
    assert list(drift["Status"]) == ["not proposed yet (the next scan will)"]

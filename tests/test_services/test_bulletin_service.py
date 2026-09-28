"""Phase 4 — bulletin_service: bulletin format, publication, scan-time snapshots, idempotent
change detection and the Option A blind spot."""

from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.db_models import AuditEvent, BulletinSnapshot, ChangeLogEntry
from models.enums import SectionChangeKind
from models.schemas import ChangeLogEntryRecord
from services import bulletin_service as svc
from services.errors import (
    BulletinNotPublishedError,
    BulletinSnapshotNotFoundError,
    ChangeLogEntryNotFoundError,
    InvalidBulletinError,
)
from tests.fixtures.compliance import BASELINE_BULLETIN, BASELINE_RULE_KEYS, edit_section

EDITOR = "bulletin-editor"
SCANNER = "watch-scheduler"

DUTY_RAISED = edit_section(
    BASELINE_BULLETIN, "HSN-8708-duty", "15% of the assessable", "20% of the assessable"
)


def _scan(session: Session) -> ChangeLogEntryRecord | None:
    """What the scheduled scan does: capture, then diff against the previous capture."""
    svc.capture_snapshot(session, SCANNER)
    return svc.diff_since_last(session, SCANNER)


def _count(session: Session, model: type) -> int | None:
    return session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------- format


def test_baseline_bulletin_parses_into_rule_sections() -> None:
    sections = svc.parse_sections(BASELINE_BULLETIN)

    assert tuple(s.rule_key for s in sections) == BASELINE_RULE_KEYS
    duty = sections[0]
    assert (duty.effective_date, duty.hs_codes) == (date(2026, 4, 1), ("8708",))
    assert duty.text.startswith("Parts and accessories")
    assert sections[2].hs_codes == ()  # "HS codes: all"


@pytest.mark.parametrize(
    ("bulletin", "problem"),
    [
        ("Just a preamble.\n", "no '## <rule_key>' sections"),
        ("## A-1\nEffective: 2026-01-01\nHS codes: all\nx\n## A-1\n"
         "Effective: 2026-01-01\nHS codes: all\ny\n", "duplicate"),
        ("## A-1\nHS codes: all\nText.\n", "missing effective"),
        ("## A-1\nEffective: 2026-02-30\nHS codes: all\nText.\n", "invalid Effective date"),
        ("## A-1\nEffective: 2026-01-01\nHS codes: 87X8\nText.\n", "HS codes must be"),
        ("## A-1\nEffective: 2026-01-01\nHS codes:\nText.\n", "HS codes must be"),
        ("## A-1\nEffective: 2026-01-01\nHS codes: 8708\n\n", "rule text is empty"),
        ("## A_1!\nEffective: 2026-01-01\nHS codes: 8708\nText.\n", "rule key"),
    ],
)  # fmt: skip
def test_malformed_bulletins_are_rejected(bulletin: str, problem: str) -> None:
    with pytest.raises(InvalidBulletinError, match=problem):
        svc.parse_sections(bulletin)


def test_section_diff_reports_added_modified_and_removed_rules() -> None:
    new = edit_section(DUTY_RAISED, "ORG-KR-CEPA", "## ORG-KR-CEPA", "## ORG-JP-CEPA")

    changes = svc.diff_bulletins(BASELINE_BULLETIN, new)

    assert [(c.rule_key, c.kind) for c in changes] == [
        ("HSN-8708-duty", SectionChangeKind.MODIFIED),
        ("ORG-JP-CEPA", SectionChangeKind.ADDED),
        ("ORG-KR-CEPA", SectionChangeKind.REMOVED),
    ]
    assert all(c.is_substantive for c in changes)
    modified = changes[0]
    assert modified.before is not None and modified.after is not None
    assert "20%" in modified.after.text and "15%" in modified.before.text


# --------------------------------------------------------------------------- publication


def test_publish_stores_the_bulletin_and_audits_it(db_session: Session) -> None:
    published = svc.publish(db_session, BASELINE_BULLETIN, EDITOR)

    assert svc.get_publication(db_session) == published
    assert (published.content, published.updated_by) == (BASELINE_BULLETIN, EDITOR)
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "bulletin.published")
    ).one()
    assert event.payload["sections"] == list(BASELINE_RULE_KEYS)
    assert event.payload["previous_sha256"] is None


def test_republishing_identical_text_is_a_no_op(db_session: Session) -> None:
    first = svc.publish(db_session, BASELINE_BULLETIN, EDITOR)

    again = svc.publish(db_session, BASELINE_BULLETIN.replace("\n", "\r\n"), "someone-else")

    assert again == first
    assert _count(db_session, AuditEvent) == 1


def test_publishing_never_captures_a_snapshot(db_session: Session) -> None:
    """Option A: only the scheduled scan captures snapshots, never a bulletin edit."""
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    svc.publish(db_session, DUTY_RAISED, EDITOR)

    assert _count(db_session, BulletinSnapshot) == 0
    with pytest.raises(BulletinSnapshotNotFoundError):
        svc.get_latest_snapshot(db_session)


def test_invalid_publication_is_rejected_and_the_old_text_kept(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)

    with pytest.raises(InvalidBulletinError):
        svc.publish(db_session, "## broken section without metadata\n", EDITOR)

    assert svc.get_publication(db_session).content == BASELINE_BULLETIN


def test_nothing_published_raises(db_session: Session) -> None:
    with pytest.raises(BulletinNotPublishedError):
        svc.get_publication(db_session)
    with pytest.raises(BulletinNotPublishedError):
        svc.capture_snapshot(db_session, SCANNER)


# --------------------------------------------------------------------------- scan path


def test_capture_records_the_published_text_in_order(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    first = svc.capture_snapshot(db_session, SCANNER)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    second = svc.capture_snapshot(db_session, SCANNER)

    assert second.seq > first.seq
    assert (first.content, second.content) == (BASELINE_BULLETIN, DUTY_RAISED)
    assert svc.get_latest_snapshot(db_session) == second
    assert svc.list_snapshots(db_session) == [second, first]


def test_first_scan_is_only_a_baseline(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)

    assert _scan(db_session) is None


def test_diff_twice_without_a_new_capture_returns_none_the_second_time(
    db_session: Session,
) -> None:
    """Exit criterion: detection is idempotent — no duplicate change_log rows."""
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    svc.capture_snapshot(db_session, SCANNER)

    first = svc.diff_since_last(db_session, SCANNER)
    second = svc.diff_since_last(db_session, SCANNER)

    assert first is not None and first.is_substantive
    assert first.diff_summary == "Substantive change.\n- HSN-8708-duty: modified"
    assert second is None
    assert _count(db_session, ChangeLogEntry) == 1


def test_rescan_of_unchanged_bulletin_detects_nothing(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    assert _scan(db_session) is not None

    assert _scan(db_session) is None
    assert _scan(db_session) is None
    assert _count(db_session, ChangeLogEntry) == 1


def test_whitespace_or_case_only_edit_is_detected_but_not_substantive(
    db_session: Session,
) -> None:
    """Edge case: a formatting-only edit is a text difference with is_substantive=False."""
    reformatted = edit_section(
        BASELINE_BULLETIN,
        "HSN-8708-duty",
        "Parts and accessories of motor vehicles (heading 8708) attract",
        "PARTS  and accessories of motor vehicles\n(heading 8708)   attract",
    )
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, reformatted, EDITOR)

    entry = _scan(db_session)

    assert entry is not None
    assert entry.is_substantive is False
    assert entry.diff_summary.startswith("Formatting-only change")
    [change] = svc.section_changes(db_session, entry.id)
    assert (change.rule_key, change.is_substantive) == ("HSN-8708-duty", False)


def test_two_substantive_changes_in_one_snapshot_are_attributed_per_rule(
    db_session: Session,
) -> None:
    both = edit_section(DUTY_RAISED, "VAL-exchange-rate", "for the period", "for the fortnight")
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, both, EDITOR)

    entry = _scan(db_session)

    assert entry is not None
    changes = svc.section_changes(db_session, entry.id)
    assert [c.rule_key for c in changes] == ["HSN-8708-duty", "VAL-exchange-rate"]
    assert all(c.is_substantive for c in changes)


def test_change_reverted_between_scans_is_invisible_by_design(db_session: Session) -> None:
    """Option A: an edit made and reverted between two scans leaves no trace."""
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)

    assert _scan(db_session) is None
    assert _count(db_session, ChangeLogEntry) == 0


def test_change_texts_and_lookup(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    entry = _scan(db_session)
    assert entry is not None

    assert svc.change_texts(db_session, entry.id) == (BASELINE_BULLETIN, DUTY_RAISED)
    assert svc.list_changes(db_session) == [entry]
    diff = svc.unified_diff(BASELINE_BULLETIN, DUTY_RAISED)
    assert "-15% of the assessable" in diff and "+20% of the assessable" in diff
    with pytest.raises(ChangeLogEntryNotFoundError):
        svc.get_change(db_session, entry.prior_snapshot_id)


# --------------------------------------------------------------------------- database guards


def test_database_keeps_snapshots_and_change_log_immutable(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    entry = _scan(db_session)
    assert entry is not None

    for statement in (
        "UPDATE bulletin.snapshots SET content = 'x' WHERE id = :snap",
        "DELETE FROM bulletin.snapshots WHERE id = :snap",
        "UPDATE bulletin.change_log SET is_substantive = false WHERE id = :entry",
        "DELETE FROM bulletin.change_log WHERE id = :entry",
    ):
        with (
            pytest.raises(IntegrityError, match=r"immutable|never deleted"),
            db_session.begin_nested(),
        ):
            db_session.execute(text(statement), {"snap": entry.new_snapshot_id, "entry": entry.id})

    # emailed_at may be set once (Phase 6), never changed afterwards
    set_emailed = (
        "UPDATE bulletin.change_log SET emailed_at = now() + CAST(:d AS interval) WHERE id = :entry"
    )
    db_session.execute(text(set_emailed), {"d": "0 s", "entry": entry.id})
    with pytest.raises(IntegrityError, match="emailed_at once"), db_session.begin_nested():
        db_session.execute(text(set_emailed), {"d": "1 hour", "entry": entry.id})


def test_database_allows_one_change_log_row_per_snapshot_pair(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)
    _scan(db_session)
    svc.publish(db_session, DUTY_RAISED, EDITOR)
    entry = _scan(db_session)
    assert entry is not None

    with (
        pytest.raises(IntegrityError, match="uq_change_log_prior_new_snapshot"),
        db_session.begin_nested(),
    ):
        db_session.add(
            ChangeLogEntry(
                prior_snapshot_id=entry.prior_snapshot_id,
                new_snapshot_id=entry.new_snapshot_id,
                diff_summary="dup",
                is_substantive=True,
            )
        )
        db_session.flush()


def test_database_allows_a_single_publication_row(db_session: Session) -> None:
    svc.publish(db_session, BASELINE_BULLETIN, EDITOR)

    with pytest.raises(IntegrityError, match="uq_publication_singleton"), db_session.begin_nested():
        db_session.execute(
            text("INSERT INTO bulletin.publication (content, updated_by) VALUES ('x', 'y')")
        )

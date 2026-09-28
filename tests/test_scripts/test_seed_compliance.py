"""Phase 4 — the compliance seed publishes the bulletin and imports the baseline rulebook once,
through the governed path, without capturing any snapshot."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from models.db_models import AuditEvent, BulletinSnapshot, ComplianceRule
from models.enums import RuleStatus
from scripts.seed_compliance import seed
from services import bulletin_service, compliance_service
from tests.fixtures.compliance import BASELINE_BULLETIN, BASELINE_RULE_KEYS, edit_section


def _counts(session: Session) -> tuple[int | None, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (ComplianceRule, BulletinSnapshot, AuditEvent)
    )


def test_seed_publishes_bulletin_and_approves_baseline_rules(
    session_factory: sessionmaker[Session],
) -> None:
    results = seed()

    assert [r.outcome for r in results] == ["created"] * (1 + len(BASELINE_RULE_KEYS))
    with session_factory() as session:
        assert bulletin_service.get_publication(session).content == BASELINE_BULLETIN
        rules = compliance_service.list_approved(session)
        assert tuple(sorted(r.rule_key for r in rules)) == tuple(sorted(BASELINE_RULE_KEYS))
        assert {(r.version, r.status, r.decided_by) for r in rules} == {
            (1, RuleStatus.APPROVED, "seed_script")
        }
        assert compliance_service.list_pending(session) == []
        assert _counts(session)[1] == 0  # Option A: only the scheduled scan captures


def test_seed_is_idempotent_and_preserves_dashboard_edits(
    session_factory: sessionmaker[Session],
) -> None:
    seed()
    edited = edit_section(
        BASELINE_BULLETIN, "HSN-8708-duty", "15% of the assessable", "20% of the assessable"
    )
    with session_factory() as session, session.begin():
        bulletin_service.publish(session, edited, "dashboard-user")
    with session_factory() as session:
        before = _counts(session)

    results = seed()

    assert {r.outcome for r in results} == {"skipped"}
    with session_factory() as session:
        assert _counts(session) == before
        assert bulletin_service.get_publication(session).content == edited

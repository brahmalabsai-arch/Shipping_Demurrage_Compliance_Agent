"""Phase 3 — the FX seed creates spot, locked and gazetted rates once and is safe to re-run."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from models.db_models import AuditEvent, GazettedRate, LockedRate, SpotRate
from models.enums import FxStage
from models.schemas import SpotRateUpsert
from scripts.seed_fx_rates import seed
from services import fx_service
from tests.fixtures.fx_rates import (
    SEED_GAZETTED_RATES,
    SEED_LOCKED_RATES,
    SEED_SPOT_RATES,
    USD,
    spot_instant,
)


def _counts(session: Session) -> tuple[int | None, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (SpotRate, LockedRate, GazettedRate, AuditEvent)
    )


def test_seed_creates_every_rate_and_links_revisions(
    session_factory: sessionmaker[Session],
) -> None:
    results = seed()

    expected = len(SEED_SPOT_RATES) + len(SEED_LOCKED_RATES) + len(SEED_GAZETTED_RATES)
    assert [r.outcome for r in results] == ["created"] * expected
    with session_factory() as session:
        assert _counts(session)[:3] == (24, 7, 26)
        revision = fx_service.get_gazetted_rate(session, USD, date(2026, 9, 22))
        original = fx_service.get_gazetted_rate(session, USD, date(2026, 9, 20))
    assert revision.revision_of == original.id
    assert (original.rate, revision.rate) == (Decimal("88.30"), Decimal("88.40"))


def test_seed_is_idempotent_and_preserves_dashboard_corrections(
    session_factory: sessionmaker[Session],
) -> None:
    seed()
    correction = SpotRateUpsert(
        pair=USD, rate=Decimal("87.6200"), as_of=spot_instant(date(2026, 8, 10)), source="desk"
    )
    with session_factory() as session, session.begin():
        fx_service.upsert_spot_rate(session, correction, "dashboard-user")
    with session_factory() as session:
        before = _counts(session)

    results = seed()

    assert {r.outcome for r in results} == {"skipped"}
    with session_factory() as session:
        assert _counts(session) == before
        kept = fx_service.resolve_rate(
            session, USD, date(2026, 8, 10), FxStage.QUOTATION, quote_validity_days=14
        )
    assert (kept.rate, kept.as_of) == (Decimal("87.6200"), date(2026, 8, 10))

"""Phase 2 — the tariff seed creates the INNSA/INMUN periods once and is safe to re-run."""

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from models.db_models import AuditEvent, PortRate
from models.enums import ContainerType
from models.schemas import PortRateUpsert
from scripts.seed_port_rates import seed
from services import demurrage_service
from tests.fixtures.port_rates import SEED_PORT_RATES


def _counts(session: Session) -> tuple[int | None, int | None]:
    return (
        session.scalar(select(func.count()).select_from(PortRate)),
        session.scalar(select(func.count()).select_from(AuditEvent)),
    )


def test_seed_creates_base_and_revision_periods(session_factory: sessionmaker[Session]) -> None:
    results = seed()

    assert [r.outcome for r in results] == ["created"] * len(SEED_PORT_RATES)
    with session_factory() as session:
        september = demurrage_service.get_rate(
            session, "INNSA", ContainerType.DRY_40, date(2026, 9, 8)
        )
        october = demurrage_service.get_rate(
            session, "INNSA", ContainerType.DRY_40, date(2026, 10, 8)
        )
    assert (september.daily_rate, september.effective_to) == (Decimal("90.00"), date(2026, 9, 30))
    assert (october.daily_rate, october.effective_to) == (Decimal("110.00"), None)


def test_seed_is_idempotent_and_preserves_dashboard_corrections(
    session_factory: sessionmaker[Session],
) -> None:
    seed()
    correction = PortRateUpsert(
        port_code="INNSA",
        container_type=ContainerType.DRY_20,
        currency="USD",
        free_days=5,
        daily_rate=Decimal("46.00"),
        effective_from=date(2026, 1, 1),
    )
    with session_factory() as session, session.begin():
        demurrage_service.upsert_rate(session, correction, "dashboard-user")
    with session_factory() as session:
        before = _counts(session)

    results = seed()

    assert {r.outcome for r in results} == {"skipped"}
    with session_factory() as session:
        assert _counts(session) == before
        kept = demurrage_service.get_rate(session, "INNSA", ContainerType.DRY_20, date(2026, 3, 1))
        assert kept.daily_rate == Decimal("46.00")

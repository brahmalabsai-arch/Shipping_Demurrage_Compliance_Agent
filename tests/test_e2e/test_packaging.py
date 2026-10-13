"""Phase 13 — end to end / packaging (``edge_cases.md`` Phase 13).

1. The JNPT demo run twice back-to-back without a reset: the second run's audit events are
   cleanly distinguishable by ``run_id`` and nothing from run 1 leaks into run 2.
2. A brand-new, empty database reaches a demoable state with only the documented commands
   (``alembic upgrade head``, ``python -m scripts.seed``) — no manual SQL, no hidden step. Runs
   the real commands in subprocesses, so the seed loads the real RAG models (marked
   ``models``; skipped in CI).
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from agents.mcp_clients import memory_connector
from config.settings import PROJECT_ROOT, get_settings
from models.db_models import (
    CHECKPOINT_SCHEMA,
    DOMAIN_SCHEMAS,
    BulletinPublication,
    ComplianceRule,
    Document,
    PortRate,
    Shipment,
)
from models.enums import RuleStatus
from models.schemas import RoiAssumptions
from rag import store
from roi.calculator import compare
from scripts import demo_jnpt, seed
from services import audit_service, roi_service
from services import db as db_module

AS_OF = date(2026, 9, 28)
CONTAINERS = {"MSKU7001001", "MSCU7002002", "CMAU7003003", "HLXU7004004"}
DEMO_TABLE = {  # container: (document findings, risk, demurrage INR, ready for clearance)
    "MSKU7001001": ([], "high", Decimal("15912.00"), True),
    "MSCU7002002": (["quantity_mismatch"], "high", Decimal("15912.00"), False),
    "CMAU7003003": (["missing_document"], "medium", Decimal("0.00"), False),
    "HLXU7004004": ([], "high", Decimal("141440.00"), True),
}
RUN_EVENTS = {"document_verification.completed", "demurrage_risk.completed",
              "compliance_rag.completed", "clearance_checklist.completed"}  # fmt: skip


# --------------------------------------------------------------------------- back to back


async def test_the_demo_twice_back_to_back_without_a_reset(
    session_factory: sessionmaker[Session],
) -> None:
    seed.main()

    first = await demo_jnpt.run_demo(as_of=AS_OF, connector=memory_connector,
                                     model_factory=lambda: None)  # fmt: skip
    second = await demo_jnpt.run_demo(as_of=AS_OF, connector=memory_connector,
                                      model_factory=lambda: None)  # fmt: skip

    # the table docs/DEMO_SCRIPT.md quotes (deterministic parts only; rulebook answers depend on
    # the retrieval models)
    assert {s.container_id: (s.documents, s.risk, s.demurrage_inr, s.ready_for_clearance)
            for s in second.shipments} == DEMO_TABLE  # fmt: skip

    runs_1 = {s.run_id for s in first.shipments}
    runs_2 = {s.run_id for s in second.shipments}
    assert len(runs_1) == len(runs_2) == 4 and runs_1.isdisjoint(runs_2)
    with session_factory() as session:
        for run_id in runs_2:  # each run's trail is its own, complete, and only its own
            events = audit_service.query_events(session, run_id=run_id)
            assert {e.action for e in events} == RUN_EVENTS
            assert all(run_id in (str(e.entity_id), e.payload.get("run_id")) for e in events)
        ids_1 = {e.id for r in runs_1 for e in audit_service.query_events(session, run_id=r)}
        ids_2 = {e.id for r in runs_2 for e in audit_service.query_events(session, run_id=r)}
        assert ids_1.isdisjoint(ids_2)

    # nothing from run 1 leaks into run 2: same inputs, same outputs (run ids aside)
    for a, b in zip(first.shipments, second.shipments, strict=True):
        assert a.container_id == b.container_id
        assert a.case.model_dump(exclude={"run_id"}) == b.case.model_dump(exclude={"run_id"})
        assert b.case.agent_failures == []
    assert first.snapshot_name != second.snapshot_name
    assert first.snapshot_sha256 == second.snapshot_sha256  # identical frozen inputs
    assert first.roi.total_avoided_inr == second.roi.total_avoided_inr == Decimal("82212.00")
    assert [line.model_dump() for line in first.roi.lines] == [
        line.model_dump() for line in second.roi.lines
    ]


# --------------------------------------------------------------------------- fresh database


@pytest.fixture
def empty_database(db_engine: Engine) -> Iterator[Engine]:
    """The test database emptied completely (as after ``setup_local_db``: no schema at all);
    afterwards migrated and emptied of rows again, as the rest of the suite expects."""
    schemas = ", ".join((*DOMAIN_SCHEMAS, CHECKPOINT_SCHEMA))
    with db_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schemas} CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
    yield db_engine
    with db_engine.begin() as conn:
        tables = conn.scalars(
            text("SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname = ANY(:s)"),
            {"s": list(DOMAIN_SCHEMAS)},
        ).all()
        if tables:
            conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


def _documented(command: list[str], chroma: Path) -> None:
    env = {**os.environ, "POSTGRES_DB": get_settings().postgres_test_db,
           "CHROMA_PATH": str(chroma), "PYTHONIOENCODING": "utf-8"}  # fmt: skip
    # the child writes UTF-8 (PYTHONIOENCODING), so read it as UTF-8, not the console code page
    done = subprocess.run([sys.executable, "-m", *command], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, encoding="utf-8", errors="replace",
                          timeout=600)  # fmt: skip
    assert done.returncode == 0, f"{command}: {done.stderr[-2000:]}"


@pytest.mark.models
def test_an_empty_database_becomes_demoable_with_the_documented_commands(
    empty_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    chroma = get_settings().chroma_path  # this test's own (autouse fixture: a tmp dir)

    _documented(["alembic", "upgrade", "head"], chroma)
    _documented(["scripts.seed"], chroma)
    _documented(["scripts.seed"], chroma)  # idempotent: a second run changes nothing

    factory = sessionmaker(bind=empty_database, expire_on_commit=False)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    with factory() as session:
        count = lambda model: session.scalar(select(func.count()).select_from(model))  # noqa: E731
        assert set(session.scalars(select(Shipment.container_id))) == CONTAINERS
        assert count(Document) == 11 and count(PortRate) == 8
        assert count(BulletinPublication) == 1
        assert session.scalar(select(func.count()).where(
            ComplianceRule.status == RuleStatus.APPROVED)) == 6  # fmt: skip
    # the RAG index was built by the seed (approvals index themselves after commit)
    store._clients.clear()
    assert store.collection().count() == 6
    # and Mission Control's ROI works on it straight away (rolled back: nothing kept)
    with factory() as session, session.begin():
        snapshot = roi_service.capture_snapshot(session, "fresh", AS_OF, RoiAssumptions(), "t")
        assert compare(snapshot, snapshot).total_avoided_inr == Decimal("82212.00")
        session.rollback()

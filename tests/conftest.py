"""Shared pytest fixtures.

Database tests run against the disposable ``POSTGRES_TEST_DB`` (never the app database):

* once per session the test DB is wiped, migrated to head, downgraded to base and migrated
  again — so every run also proves the migrations are reversible;
* every test then runs inside one outer transaction that is rolled back afterwards
  (conventions §7 — no shared mutable DB state between tests). Service code that commits via
  ``session_scope()`` only releases a SAVEPOINT inside that transaction.

Any test using these fixtures is auto-marked ``db`` (CI runs it in the Postgres job).

The supervisor's LangGraph checkpoints (Phase 11) are written on their own autocommit connection
and cannot be rolled back: the ``checkpoints`` fixture points them at the test database and
empties the checkpoint tables afterwards.
"""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from agents import checkpoint
from config.settings import PROJECT_ROOT, get_settings
from models.db_models import CHECKPOINT_SCHEMA, DOMAIN_SCHEMAS
from rag import models as rag_models
from services import db as db_module
from tests.fixtures.rag import FakeEmbedder, FakeReranker

DB_FIXTURES = {"db_engine", "db_connection", "session_factory", "db_session", "checkpoints"}
# The registered reviewers every test sees (``REVIEWERS``); tests decide as these people.
TEST_REVIEWERS = "Asha Rao <asha.rao@example.com>; Vikram Shah <vikram.shah@example.com>"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if DB_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)


def _alembic(connection: Connection) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture(autouse=True)
def _no_external_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may reach a paid or real external service: the LLM keys from ``.env`` are
    cleared (agents use their deterministic fallbacks unless a test injects a fake model) and
    email goes to a per-test outbox directory (never Gmail)."""
    try:
        settings = get_settings()
    except ValidationError:  # CI's unit job has no .env; nothing to neutralise
        return
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "email_backend", "outbox")
    monkeypatch.setattr(settings, "outbox_path", tmp_path / "outbox")
    monkeypatch.setattr(settings, "reviewers", TEST_REVIEWERS)


@pytest.fixture(autouse=True)
def _isolated_rag_index(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every test gets its own empty Chroma directory (never ``./chroma_data``), and the fake
    embedder/reranker (``tests/fixtures/rag.py``) unless it is marked ``models`` — then the real
    BGE / MiniLM models are used (downloaded on first use; skipped in CI)."""
    try:
        settings = get_settings()
    except ValidationError:
        return
    monkeypatch.setattr(settings, "chroma_path", tmp_path / "chroma")
    if request.node.get_closest_marker("models") is None:
        embedder, reranker = FakeEmbedder(), FakeReranker()
        monkeypatch.setattr(rag_models, "get_embedder", lambda: embedder)
        monkeypatch.setattr(rag_models, "get_reranker", lambda: reranker)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_engine(get_settings().test_database_url)
    with engine.begin() as conn:
        schemas = ", ".join((*DOMAIN_SCHEMAS, CHECKPOINT_SCHEMA))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schemas} CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
    with engine.begin() as conn:
        cfg = _alembic(conn)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_connection(db_engine: Engine) -> Iterator[Connection]:
    with db_engine.connect() as conn:
        outer = conn.begin()
        try:
            yield conn
        finally:
            outer.rollback()


@pytest.fixture
def session_factory(
    db_connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    """Sessions bound to the rolled-back test connection; also installed as the factory behind
    ``services.db.session_scope`` so dashboards and scripts under test hit the same transaction.

    All sessions share ONE connection (each is a SAVEPOINT on it), but the pipeline's parallel
    branches run MCP tools on several worker threads at once — interleaved savepoints on one
    connection fail ("savepoint does not exist"). So a session's outermost transaction holds a
    re-entrant lock until it ends: one test transaction at a time, across threads. (Production
    sessions each get their own pooled connection and need no such lock.)"""
    factory = sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    lock = threading.RLock()

    @event.listens_for(factory, "after_transaction_create")
    def _hold_connection(session: Session, transaction: SessionTransaction) -> None:
        if transaction.parent is None:
            lock.acquire()

    @event.listens_for(factory, "after_transaction_end")
    def _release_connection(session: Session, transaction: SessionTransaction) -> None:
        if transaction.parent is None:
            lock.release()

    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    return factory


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def checkpoints(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Supervisor checkpoints and run locks on the test database, emptied after the test."""
    monkeypatch.setattr(checkpoint, "DATABASE", get_settings().postgres_test_db)
    yield
    with db_engine.begin() as conn:
        tables = conn.scalars(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = :schema"
                " AND tablename <> 'checkpoint_migrations'"
            ),
            {"schema": CHECKPOINT_SCHEMA},
        ).all()
        if tables:
            names = ", ".join(f"{CHECKPOINT_SCHEMA}.{t}" for t in tables)
            conn.execute(text(f"TRUNCATE {names}"))

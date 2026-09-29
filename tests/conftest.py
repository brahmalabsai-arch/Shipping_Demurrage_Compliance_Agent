"""Shared pytest fixtures.

Database tests run against the disposable ``POSTGRES_TEST_DB`` (never the app database):

* once per session the test DB is wiped, migrated to head, downgraded to base and migrated
  again — so every run also proves the migrations are reversible;
* every test then runs inside one outer transaction that is rolled back afterwards
  (conventions §7 — no shared mutable DB state between tests). Service code that commits via
  ``session_scope()`` only releases a SAVEPOINT inside that transaction.

Any test using these fixtures is auto-marked ``db`` (CI runs it in the Postgres job).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config.settings import PROJECT_ROOT, get_settings
from models.db_models import DOMAIN_SCHEMAS
from rag import models as rag_models
from services import db as db_module
from tests.fixtures.rag import FakeEmbedder, FakeReranker

DB_FIXTURES = {"db_engine", "db_connection", "session_factory", "db_session"}


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
        conn.execute(text(f"DROP SCHEMA IF EXISTS {', '.join(DOMAIN_SCHEMAS)} CASCADE"))
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
    ``services.db.session_scope`` so dashboards and scripts under test hit the same transaction."""
    factory = sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    return factory


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session

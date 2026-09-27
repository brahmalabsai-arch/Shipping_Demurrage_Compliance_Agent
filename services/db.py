"""Database engine and transaction scope shared by dashboards, MCP servers and scripts.

Service functions take a ``Session`` and never commit; the caller owns the transaction via
:func:`session_scope`, so a mutation and its ``audit.events`` row always commit together
(conventions §3).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine for the application database."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Session factory bound to :func:`get_engine`; tests replace it to bind a rollback-only
    connection."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One unit of work: commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        with session.begin():
            yield session
    finally:
        session.close()

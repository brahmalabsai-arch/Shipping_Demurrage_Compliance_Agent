"""The supervisor's LangGraph checkpointer: Postgres, same instance as the app (Phase 11).

A supervised run persists every step in the ``checkpoints`` schema (``thread_id = run_id``), so
a run interrupted at a human gate survives the process that started it — any later process
(dashboard, CLI) resumes it from there.

* :class:`ThreadedPostgresSaver` is LangGraph's **sync** ``PostgresSaver`` with its async
  methods run in worker threads. The async saver needs psycopg's async connections, which do not
  work on Windows' default Proactor event loop — and the agents' stdio MCP subprocesses need
  that loop. One psycopg connection per saver, serialised by the saver's own lock.
* :func:`serializer` only deserializes the classes ``CaseState`` is made of (an explicit
  allowlist, derived from the model — nothing else found in a checkpoint is instantiated).
* :func:`run_lock` is a Postgres advisory lock per ``run_id`` on its own connection, so two
  resumptions of one run (two reviewers deciding two pending gates at once, in any process) are
  applied one after the other, each against the checkpoint the other left.

The schema is created by migration ``0008``; the tables by the library (``setup()``, idempotent,
run on every open).
"""

import asyncio
import enum
import typing
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from pydantic import BaseModel

from agents.state import CaseState
from config.settings import get_settings
from models.db_models import CHECKPOINT_SCHEMA

# The database holding the checkpoints and run locks; None = the app database. The pytest
# harness points it at the test database.
DATABASE: str | None = None


class ThreadedPostgresSaver(PostgresSaver):
    """``PostgresSaver`` usable from async graphs: each async call runs the sync one in a
    thread (the saver's lock serialises use of its connection)."""

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        found = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in found:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


def _classes(annotation: Any, seen: set[type]) -> None:
    for arg in typing.get_args(annotation):
        _classes(arg, seen)
    if not isinstance(annotation, type) or annotation in seen:
        return
    if issubclass(annotation, enum.Enum):
        seen.add(annotation)
    elif issubclass(annotation, BaseModel):
        seen.add(annotation)
        for field in annotation.model_fields.values():
            _classes(field.annotation, seen)


def checkpoint_classes() -> list[tuple[str, str]]:
    """(module, name) of every model/enum a ``CaseState`` checkpoint can contain."""
    seen: set[type] = set()
    _classes(CaseState, seen)
    return sorted((cls.__module__, cls.__qualname__) for cls in seen)


def serializer() -> JsonPlusSerializer:
    """The checkpoint serializer, restricted to ``CaseState``'s own classes."""
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_classes())


def _connect(*, search_path: str | None) -> psycopg.Connection[Any]:
    settings = get_settings()
    options: dict[str, Any] = {"options": f"-c search_path={search_path}"} if search_path else {}
    return psycopg.Connection.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        dbname=DATABASE or settings.postgres_db,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        **options,
    )


@contextmanager
def open_checkpointer() -> Iterator[ThreadedPostgresSaver]:
    """A saver on its own connection, its tables ensured (``setup()`` is idempotent)."""
    with _connect(search_path=CHECKPOINT_SCHEMA) as conn:
        saver = ThreadedPostgresSaver(conn, serde=serializer())
        saver.setup()
        yield saver


@asynccontextmanager
async def run_lock(run_id: str) -> AsyncIterator[None]:
    """Hold the advisory lock of ``run_id``, waiting (in a thread) for any other holder — in
    this process or another. Closing the lock's own connection releases it, even on errors."""
    conn = await asyncio.to_thread(_connect, search_path=None)
    try:
        await asyncio.to_thread(
            conn.execute, "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (run_id,)
        )
        yield
    finally:
        await asyncio.to_thread(conn.close)

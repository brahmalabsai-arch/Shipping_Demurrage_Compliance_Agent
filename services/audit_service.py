"""Audit trail: every agent action and human decision becomes an ``audit.events`` row.

``record`` joins the caller's session, so the event commits (or rolls back) atomically with the
change it describes — never a separate best-effort write (conventions §3). ``query_events``
(Phase 12) reads the trail back for Mission Control: filter by run, actor, action, entity and
time, newest first.
"""

from contextlib import suppress
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic_core import to_jsonable_python
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models.db_models import AuditEvent
from models.schemas import AuditEventRecord

MAX_EVENTS = 1000  # one query never returns more (the page shows the newest first)


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Append an audit event inside the caller's transaction.

    ``payload`` may contain Decimals, dates, UUIDs or enums; it is normalised to JSON here so
    callers can pass model dumps directly.
    """
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=to_jsonable_python(payload),
        )
    )


def query_events(
    session: Session,
    *,
    run_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    action_prefix: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[AuditEventRecord]:
    """Audit events matching every given filter, newest first (at most ``MAX_EVENTS``).

    ``run_id`` matches the events of one agent/supervisor run: the ``agent_run`` events whose
    entity is the run, and any event that carries the run id in its payload (e.g. a filing
    decision, a supervised status change). ``action_prefix`` matches e.g. ``"filing."``;
    ``since`` is inclusive, ``until`` exclusive.
    """
    query = select(AuditEvent)
    if run_id is not None:
        conditions = [AuditEvent.payload["run_id"].astext == run_id]
        with suppress(ValueError):  # not a UUID: only payload matches
            conditions.append(AuditEvent.entity_id == UUID(run_id))
        query = query.where(or_(*conditions))
    if actor is not None:
        query = query.where(AuditEvent.actor == actor)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    if action_prefix is not None:
        query = query.where(AuditEvent.action.startswith(action_prefix, autoescape=True))
    if entity_type is not None:
        query = query.where(AuditEvent.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(AuditEvent.entity_id == entity_id)
    if since is not None:
        query = query.where(AuditEvent.created_at >= since)
    if until is not None:
        query = query.where(AuditEvent.created_at < until)
    query = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id).limit(
        max(1, min(limit, MAX_EVENTS))
    )
    return [AuditEventRecord.model_validate(row) for row in session.scalars(query)]


def list_actors(session: Session) -> list[str]:
    """Every actor that appears in the trail (agents, services, reviewers), sorted."""
    return list(session.scalars(select(AuditEvent.actor).distinct().order_by(AuditEvent.actor)))


def list_entity_types(session: Session) -> list[str]:
    """Every entity type that appears in the trail, sorted."""
    query = select(AuditEvent.entity_type).distinct().order_by(AuditEvent.entity_type)
    return list(session.scalars(query))

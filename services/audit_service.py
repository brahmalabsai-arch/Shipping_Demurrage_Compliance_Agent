"""Audit trail: every agent action and human decision becomes an ``audit.events`` row.

``record`` joins the caller's session, so the event commits (or rolls back) atomically with the
change it describes — never a separate best-effort write (conventions §3). Querying the trail
(``query_events``) arrives with Mission Control in Phase 12.
"""

from typing import Any
from uuid import UUID

from pydantic_core import to_jsonable_python
from sqlalchemy.orm import Session

from models.db_models import AuditEvent


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

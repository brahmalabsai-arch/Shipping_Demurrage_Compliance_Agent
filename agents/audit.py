"""Audit events for an agent's own decisions (run outcome, failures it handled).

Domain changes are audited by the services behind the MCP tools; this records what the agent
itself concluded or could not do — e.g. an email that failed after its proposal was committed
(architecture §7). Each event is its own transaction, so it survives whatever happens next.
The entity is the agent run (``entity_type='agent_run'``, ``entity_id`` = the run id).
"""

from typing import Any
from uuid import UUID

from services import audit_service
from services.db import session_scope


def record_run_event(actor: str, run_id: str, action: str, payload: dict[str, Any]) -> None:
    """Append one ``agent_run`` audit event, committed immediately."""
    with session_scope() as session:
        audit_service.record(
            session,
            actor=actor,
            action=action,
            entity_type="agent_run",
            entity_id=UUID(run_id),
            payload={"run_id": run_id, **payload},
        )

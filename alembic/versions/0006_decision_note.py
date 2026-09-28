"""Phase 6 — a reason recorded with every compliance decision (``decision_note``).

A rejection must say why (required, enforced here in the trigger as well as in the service);
an approval may carry a note. The note is written once, together with the decision, and is
immutable afterwards like ``decided_by`` / ``decided_at``. (No ``0005``: Phase 5 changed no
schema.)

Revision ID: 0006
Revises: 0004
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEAD = """
CREATE OR REPLACE FUNCTION compliance.guard_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compliance.rules rows are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
"""
_TAIL = """
    IF NEW.superseded_by IS DISTINCT FROM OLD.superseded_by
            AND (OLD.superseded_by IS NOT NULL OR NEW.status <> 'approved') THEN
        RAISE EXCEPTION 'only an approved compliance rule can be superseded, once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$
"""
_IMMUTABLE = """
    IF (NEW.id, NEW.rule_key, NEW.version, NEW.content, NEW.hs_codes, NEW.effective_date,
        NEW.change_summary, NEW.source_diff_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.rule_key, OLD.version, OLD.content, OLD.hs_codes, OLD.effective_date,
        OLD.change_summary, OLD.source_diff_id, OLD.created_at) THEN
        RAISE EXCEPTION 'compliance rule % is immutable: propose a new version', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
"""

GUARD_RULE_WITH_NOTE = (
    _HEAD
    + """
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
                OR NEW.decision_note IS NOT NULL OR NEW.superseded_by IS NOT NULL THEN
            RAISE EXCEPTION 'a compliance rule must be created pending and undecided'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
"""
    + _IMMUTABLE
    + """
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status <> 'pending' THEN
            RAISE EXCEPTION 'compliance rule % is already %', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.decided_by IS NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'a decision on compliance rule % must record who and when', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.status = 'rejected' AND coalesce(btrim(NEW.decision_note), '') = '' THEN
            RAISE EXCEPTION 'rejecting compliance rule % needs a reason', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF (NEW.decided_by, NEW.decided_at, NEW.decision_note)
          IS DISTINCT FROM (OLD.decided_by, OLD.decided_at, OLD.decision_note) THEN
        RAISE EXCEPTION 'decision fields of compliance rule % are immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
"""
    + _TAIL
)

# The 0004 version, restored on downgrade.
GUARD_RULE_0004 = (
    _HEAD
    + """
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
                OR NEW.superseded_by IS NOT NULL THEN
            RAISE EXCEPTION 'a compliance rule must be created pending and undecided'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
"""
    + _IMMUTABLE
    + """
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status <> 'pending' THEN
            RAISE EXCEPTION 'compliance rule % is already %', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.decided_by IS NULL OR NEW.decided_at IS NULL THEN
            RAISE EXCEPTION 'a decision on compliance rule % must record who and when', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF (NEW.decided_by, NEW.decided_at) IS DISTINCT FROM (OLD.decided_by, OLD.decided_at) THEN
        RAISE EXCEPTION 'decision fields of compliance rule % are immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
"""
    + _TAIL
)


def upgrade() -> None:
    op.add_column(
        "rules", sa.Column("decision_note", sa.Text(), nullable=True), schema="compliance"
    )
    op.execute(GUARD_RULE_WITH_NOTE)


def downgrade() -> None:
    op.execute(GUARD_RULE_0004)
    op.drop_column("rules", "decision_note", schema="compliance")

"""Phase 11 — the supervisor's human gates: governance tables, revocation, clearance guard.

* ``compliance.rules``: an approved version in force can be **revoked** (new enum value
  ``revoked`` + ``revoked_by`` / ``revoked_at`` / ``revoke_note``). The original decision fields
  never change; the version it had superseded may then be reinstated (``superseded_by`` back to
  NULL) — the only case in which a superseded rule comes back. ``guard_rule()`` v3 enforces it.
* ``governance.case_runs``: one row per supervised clearance run (which gate it waits at);
  at most one open run per shipment (partial unique index).
* ``governance.filing_decisions``: a reviewer's approve/reject at a run's filing gate —
  append-only, one per run, an approval only for a checklist that was ready for clearance.
* ``shipping.shipments``: a trigger refuses ``status -> cleared`` unless an approved filing
  decision exists for the shipment (``shipping_service.clear_shipment`` is the only code path).
* ``checkpoints``: the schema for LangGraph's ``PostgresSaver``; its tables are created and
  migrated by the library (``setup()``), outside Alembic (``alembic/env.py`` ignores them).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN_STATES = "state IN ('running', 'awaiting_compliance', 'awaiting_filing')"
CURRENT_APPROVED = "status = 'approved' AND superseded_by IS NULL"

GUARD_RULE_WITH_REVOCATION = """
CREATE OR REPLACE FUNCTION compliance.guard_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compliance.rules rows are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
                OR NEW.decision_note IS NOT NULL OR NEW.superseded_by IS NOT NULL
                OR NEW.revoked_by IS NOT NULL OR NEW.revoked_at IS NOT NULL
                OR NEW.revoke_note IS NOT NULL THEN
            RAISE EXCEPTION 'a compliance rule must be created pending and undecided'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.id, NEW.rule_key, NEW.version, NEW.content, NEW.hs_codes, NEW.effective_date,
        NEW.change_summary, NEW.source_diff_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.rule_key, OLD.version, OLD.content, OLD.hs_codes, OLD.effective_date,
        OLD.change_summary, OLD.source_diff_id, OLD.created_at) THEN
        RAISE EXCEPTION 'compliance rule % is immutable: propose a new version', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status AND OLD.status = 'pending' THEN
        -- the decision: once, by a named reviewer; a rejection gives its reason
        IF NEW.status NOT IN ('approved', 'rejected') THEN
            RAISE EXCEPTION 'a pending compliance rule can only be approved or rejected'
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
    ELSIF NEW.status IS DISTINCT FROM OLD.status AND OLD.status = 'approved'
            AND NEW.status = 'revoked' THEN
        -- the revocation: only of the version in force, recorded beside the decision
        IF OLD.superseded_by IS NOT NULL OR NEW.superseded_by IS NOT NULL THEN
            RAISE EXCEPTION 'only the compliance rule version in force can be revoked'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF coalesce(btrim(NEW.revoked_by), '') = '' OR NEW.revoked_at IS NULL
                OR coalesce(btrim(NEW.revoke_note), '') = '' THEN
            RAISE EXCEPTION 'revoking compliance rule % must record who, when and why', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'compliance rule % is already %', OLD.id, OLD.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF (NEW.decided_by, NEW.decided_at, NEW.decision_note)
            IS DISTINCT FROM (OLD.decided_by, OLD.decided_at, OLD.decision_note)
            AND OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'decision fields of compliance rule % are immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.revoked_by, NEW.revoked_at, NEW.revoke_note)
            IS DISTINCT FROM (OLD.revoked_by, OLD.revoked_at, OLD.revoke_note)
            AND NOT (OLD.status = 'approved' AND NEW.status = 'revoked') THEN
        RAISE EXCEPTION 'revocation fields of compliance rule % are set only by revoking it',
            OLD.id USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
        IF OLD.superseded_by IS NULL THEN
            IF NEW.status <> 'approved' THEN
                RAISE EXCEPTION 'only an approved compliance rule can be superseded'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSIF NEW.superseded_by IS NOT NULL OR NEW.status <> 'approved' OR NOT EXISTS (
                SELECT 1 FROM compliance.rules r
                WHERE r.id = OLD.superseded_by AND r.status = 'revoked') THEN
            RAISE EXCEPTION 'a superseded compliance rule is reinstated only when its successor '
                'is revoked' USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

GUARD_CLEARANCE = """
CREATE FUNCTION shipping.guard_clearance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'cleared' AND OLD.status IS DISTINCT FROM NEW.status AND NOT EXISTS (
            SELECT 1 FROM governance.filing_decisions f
            WHERE f.shipment_id = NEW.id AND f.decision = 'approve') THEN
        RAISE EXCEPTION 'shipment % can be cleared only with an approved filing decision',
            NEW.container_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$
"""

REJECT_MUTATION = """
CREATE FUNCTION governance.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'governance.% rows are immutable: % rejected', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$
"""


def _guard_rule_0006() -> str:
    """The 0006 trigger body, restored on downgrade (loaded from that migration's file)."""
    path = Path(__file__).with_name("0006_decision_note.py")
    spec = importlib.util.spec_from_file_location("migration_0006", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.GUARD_RULE_WITH_NOTE)


def upgrade() -> None:
    # --- compliance: revocation
    op.execute("ALTER TYPE compliance.rule_status ADD VALUE IF NOT EXISTS 'revoked'")
    for column, type_ in (
        ("revoked_by", sa.Text()),
        ("revoked_at", sa.DateTime(timezone=True)),
        ("revoke_note", sa.Text()),
    ):
        op.add_column("rules", sa.Column(column, type_, nullable=True), schema="compliance")
    op.execute(GUARD_RULE_WITH_REVOCATION)

    # --- governance
    op.execute("CREATE SCHEMA IF NOT EXISTS governance")
    run_state = postgresql.ENUM(
        "running", "awaiting_compliance", "awaiting_filing", "filed", "filing_rejected",
        "blocked", "not_fileable", "stopped", "failed",
        name="run_state", schema="governance", create_type=False,
    )  # fmt: skip
    decision = postgresql.ENUM(
        "approve", "reject", name="decision", schema="governance", create_type=False
    )
    run_state.create(op.get_bind())
    decision.create(op.get_bind())

    op.create_table(
        "case_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("state", run_state, nullable=False),
        sa.Column(
            "awaiting_rule_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipping.shipments.id"],
            name=op.f("fk_case_runs_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_case_runs")),
        schema="governance",
    )
    op.create_index(
        op.f("ix_governance_case_runs_shipment_id"),
        "case_runs",
        ["shipment_id"],
        schema="governance",
    )
    op.create_index(
        "uq_case_runs_open_shipment",
        "case_runs",
        ["shipment_id"],
        unique=True,
        schema="governance",
        postgresql_where=sa.text(OPEN_STATES),
    )

    op.create_table(
        "filing_decisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("decision", decision, nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("ready_for_clearance", sa.Boolean(), nullable=False),
        sa.Column("checklist", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(reviewer) <> ''", name=op.f("ck_filing_decisions_names_reviewer")
        ),
        sa.CheckConstraint(
            "decision <> 'reject' OR coalesce(btrim(note), '') <> ''",
            name=op.f("ck_filing_decisions_rejection_has_reason"),
        ),
        sa.CheckConstraint(
            "decision <> 'approve' OR ready_for_clearance",
            name=op.f("ck_filing_decisions_approval_needs_ready_case"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["governance.case_runs.run_id"],
            name=op.f("fk_filing_decisions_run_id_case_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipping.shipments.id"],
            name=op.f("fk_filing_decisions_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_filing_decisions")),
        sa.UniqueConstraint("run_id", name=op.f("uq_filing_decisions_run_id")),
        schema="governance",
    )
    op.create_index(
        op.f("ix_governance_filing_decisions_shipment_id"),
        "filing_decisions",
        ["shipment_id"],
        schema="governance",
    )
    op.execute(REJECT_MUTATION)
    op.execute(
        "CREATE TRIGGER trg_filing_decisions_immutable BEFORE UPDATE OR DELETE "
        "ON governance.filing_decisions FOR EACH ROW EXECUTE FUNCTION governance.reject_mutation()"
    )

    # --- shipping: `cleared` only with an approved filing decision
    op.execute(GUARD_CLEARANCE)
    op.execute(
        "CREATE TRIGGER trg_shipments_clearance BEFORE UPDATE OF status ON shipping.shipments "
        "FOR EACH ROW EXECUTE FUNCTION shipping.guard_clearance()"
    )

    # --- LangGraph checkpoints (tables created by PostgresSaver.setup())
    op.execute("CREATE SCHEMA IF NOT EXISTS checkpoints")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS checkpoints CASCADE")

    op.execute("DROP TRIGGER IF EXISTS trg_shipments_clearance ON shipping.shipments")
    op.execute("DROP FUNCTION IF EXISTS shipping.guard_clearance()")

    op.drop_table("filing_decisions", schema="governance")  # drops its trigger with it
    op.drop_table("case_runs", schema="governance")
    op.execute("DROP FUNCTION IF EXISTS governance.reject_mutation()")
    postgresql.ENUM(name="decision", schema="governance").drop(op.get_bind())
    postgresql.ENUM(name="run_state", schema="governance").drop(op.get_bind())
    op.execute("DROP SCHEMA IF EXISTS governance")

    # A revoked rule cannot be represented before 0008: the cast below fails if any exists.
    op.execute(_guard_rule_0006())
    for column in ("revoke_note", "revoked_at", "revoked_by"):
        op.drop_column("rules", column, schema="compliance")
    op.drop_index("uq_rules_current_approved", table_name="rules", schema="compliance")
    op.execute("ALTER TYPE compliance.rule_status RENAME TO rule_status_0008")
    op.execute("CREATE TYPE compliance.rule_status AS ENUM ('approved', 'pending', 'rejected')")
    op.execute(
        "ALTER TABLE compliance.rules ALTER COLUMN status TYPE compliance.rule_status "
        "USING status::text::compliance.rule_status"
    )
    op.execute("DROP TYPE compliance.rule_status_0008")
    op.create_index(
        "uq_rules_current_approved",
        "rules",
        ["rule_key"],
        unique=True,
        schema="compliance",
        postgresql_where=sa.text(CURRENT_APPROVED),
    )

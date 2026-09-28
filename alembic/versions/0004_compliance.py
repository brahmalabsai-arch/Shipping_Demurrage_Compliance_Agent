"""Phase 4 — compliance rulebook and the simulated customs bulletin.

Triggers enforce the review lifecycle in Postgres, so it holds even for writes that bypass the
service layer:

* ``compliance.guard_rule()`` — a rule is inserted ``pending`` and undecided; its status
  changes once, from ``pending``, together with ``decided_by``/``decided_at``; only an approved
  rule gets ``superseded_by``, once; the rule text and metadata never change; no deletes.
* ``bulletin.reject_mutation()`` — snapshots are append-only.
* ``bulletin.guard_change_log()`` — only ``emailed_at`` may be set (once); no deletes.

All raise SQLSTATE 23001 (restrict_violation), which SQLAlchemy surfaces as IntegrityError.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RULE_STATUSES = ("approved", "pending", "rejected")


def _uuid_pk() -> sa.Column[sa.Uuid[object]]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamp(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


GUARD_RULE = """
CREATE FUNCTION compliance.guard_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'compliance.rules rows are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
                OR NEW.superseded_by IS NOT NULL THEN
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
    IF NEW.superseded_by IS DISTINCT FROM OLD.superseded_by
            AND (OLD.superseded_by IS NOT NULL OR NEW.status <> 'approved') THEN
        RAISE EXCEPTION 'only an approved compliance rule can be superseded, once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$
"""

REJECT_SNAPSHOT_MUTATION = """
CREATE FUNCTION bulletin.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'bulletin.% rows are immutable: % rejected', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$
"""

GUARD_CHANGE_LOG = """
CREATE FUNCTION bulletin.guard_change_log() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bulletin.change_log rows are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.prior_snapshot_id, NEW.new_snapshot_id, NEW.diff_summary,
        NEW.is_substantive, NEW.detected_at)
       IS DISTINCT FROM
       (OLD.id, OLD.prior_snapshot_id, OLD.new_snapshot_id, OLD.diff_summary,
        OLD.is_substantive, OLD.detected_at)
       OR (OLD.emailed_at IS NOT NULL AND NEW.emailed_at IS DISTINCT FROM OLD.emailed_at) THEN
        RAISE EXCEPTION 'bulletin.change_log % is immutable except for setting emailed_at once',
            OLD.id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS bulletin")
    op.execute("CREATE SCHEMA IF NOT EXISTS compliance")
    rule_status = postgresql.ENUM(*RULE_STATUSES, name="rule_status", schema="compliance")

    op.create_table(
        "publication",
        _uuid_pk(),
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _timestamp("updated_at"),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication")),
        sa.UniqueConstraint("singleton", name=op.f("uq_publication_singleton")),
        sa.CheckConstraint("singleton", name=op.f("ck_publication_single_row")),
        schema="bulletin",
    )

    op.create_table(
        "snapshots",
        _uuid_pk(),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _timestamp("captured_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshots")),
        sa.UniqueConstraint("seq", name=op.f("uq_snapshots_seq")),
        schema="bulletin",
    )

    op.create_table(
        "change_log",
        _uuid_pk(),
        sa.Column("prior_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("new_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("diff_summary", sa.Text(), nullable=False),
        sa.Column("is_substantive", sa.Boolean(), nullable=False),
        _timestamp("detected_at"),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_log")),
        sa.ForeignKeyConstraint(
            ["prior_snapshot_id"],
            ["bulletin.snapshots.id"],
            name=op.f("fk_change_log_prior_snapshot_id_snapshots"),
        ),
        sa.ForeignKeyConstraint(
            ["new_snapshot_id"],
            ["bulletin.snapshots.id"],
            name=op.f("fk_change_log_new_snapshot_id_snapshots"),
        ),
        sa.UniqueConstraint(
            "prior_snapshot_id", "new_snapshot_id", name="uq_change_log_prior_new_snapshot"
        ),
        sa.CheckConstraint(
            "prior_snapshot_id <> new_snapshot_id", name=op.f("ck_change_log_distinct_snapshots")
        ),
        schema="bulletin",
    )

    op.create_table(
        "rules",
        _uuid_pk(),
        sa.Column("rule_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", rule_status, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("hs_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("source_diff_id", sa.Uuid(), nullable=True),
        _timestamp("created_at"),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")),
        sa.ForeignKeyConstraint(
            ["superseded_by"], ["compliance.rules.id"], name=op.f("fk_rules_superseded_by_rules")
        ),
        sa.ForeignKeyConstraint(
            ["source_diff_id"],
            ["bulletin.change_log.id"],
            name=op.f("fk_rules_source_diff_id_change_log"),
        ),
        sa.UniqueConstraint("rule_key", "version", name="uq_rules_rule_key_version"),
        sa.CheckConstraint("version >= 1", name=op.f("ck_rules_positive_version")),
        schema="compliance",
    )
    op.create_index(
        "uq_rules_current_approved",
        "rules",
        ["rule_key"],
        unique=True,
        schema="compliance",
        postgresql_where=sa.text("status = 'approved' AND superseded_by IS NULL"),
    )

    op.execute(GUARD_RULE)
    op.execute(
        "CREATE TRIGGER trg_rules_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON "
        "compliance.rules FOR EACH ROW EXECUTE FUNCTION compliance.guard_rule()"
    )
    op.execute(REJECT_SNAPSHOT_MUTATION)
    op.execute(
        "CREATE TRIGGER trg_snapshots_immutable BEFORE UPDATE OR DELETE ON bulletin.snapshots "
        "FOR EACH ROW EXECUTE FUNCTION bulletin.reject_mutation()"
    )
    op.execute(GUARD_CHANGE_LOG)
    op.execute(
        "CREATE TRIGGER trg_change_log_guard BEFORE UPDATE OR DELETE ON bulletin.change_log "
        "FOR EACH ROW EXECUTE FUNCTION bulletin.guard_change_log()"
    )


def downgrade() -> None:
    op.drop_table("rules", schema="compliance")  # drops its index and trigger with it
    op.drop_table("change_log", schema="bulletin")
    op.drop_table("snapshots", schema="bulletin")
    op.drop_table("publication", schema="bulletin")
    op.execute("DROP FUNCTION IF EXISTS compliance.guard_rule()")
    op.execute("DROP FUNCTION IF EXISTS bulletin.reject_mutation()")
    op.execute("DROP FUNCTION IF EXISTS bulletin.guard_change_log()")
    postgresql.ENUM(name="rule_status", schema="compliance").drop(op.get_bind())
    op.execute("DROP SCHEMA IF EXISTS compliance")
    op.execute("DROP SCHEMA IF EXISTS bulletin")

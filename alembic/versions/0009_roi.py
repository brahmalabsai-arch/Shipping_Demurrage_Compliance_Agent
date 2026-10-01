"""Phase 12 — ROI snapshots: the frozen inputs of a before/after ROI comparison.

``roi.snapshots`` holds named, append-only copies (JSONB, with their SHA-256) of everything the
ROI calculator reads — shipments and their current documents, the tariff and FX rate pricing
each one, the rulebook in force, and the scenario assumptions — so a comparison never reads
live data and can be re-run later on exactly the same inputs (conventions §9).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REJECT_MUTATION = """
CREATE FUNCTION roi.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'roi.% rows are immutable: % rejected', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS roi")
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("captured_by", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshots")),
        sa.UniqueConstraint("name", name=op.f("uq_snapshots_name")),
        schema="roi",
    )
    op.execute(REJECT_MUTATION)
    op.execute(
        "CREATE TRIGGER trg_snapshots_immutable BEFORE UPDATE OR DELETE ON roi.snapshots "
        "FOR EACH ROW EXECUTE FUNCTION roi.reject_mutation()"
    )


def downgrade() -> None:
    op.drop_table("snapshots", schema="roi")  # drops its trigger with it
    op.execute("DROP FUNCTION IF EXISTS roi.reject_mutation()")
    op.execute("DROP SCHEMA IF EXISTS roi")

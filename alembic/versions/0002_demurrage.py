"""Phase 2 — demurrage port rates.

Tariff periods per (port_code, container_type) are kept non-overlapping by a GiST exclusion
constraint over ``daterange(effective_from, effective_to, '[]')``, which needs the
``btree_gist`` extension (trusted since PG13, so the database owner may create it).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE SCHEMA IF NOT EXISTS demurrage")

    op.create_table(
        "port_rates",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("port_code", sa.Text(), nullable=False),
        sa.Column("container_type", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("free_days", sa.Integer(), nullable=False),
        sa.Column("daily_rate", sa.Numeric(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_port_rates")),
        sa.CheckConstraint(
            "free_days >= 0 AND daily_rate >= 0", name=op.f("ck_port_rates_non_negative")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_port_rates_valid_period"),
        ),
        postgresql.ExcludeConstraint(
            ("port_code", "="),
            ("container_type", "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="ex_port_rates_no_overlap",
        ),
        schema="demurrage",
    )


def downgrade() -> None:
    op.drop_table("port_rates", schema="demurrage")
    op.execute("DROP SCHEMA IF EXISTS demurrage")
    # btree_gist is left installed: it is database-wide and harmless.

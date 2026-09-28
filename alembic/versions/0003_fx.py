"""Phase 3 — FX rates: spot, locked and gazetted tables.

``locked_rates`` and ``gazetted_rates`` are append-only: a trigger rejects every UPDATE and
DELETE, so immutability holds even for writes that bypass the service layer. Gazetted originals
of one pair never overlap, and revisions of one row never overlap each other (two partial GiST
exclusion constraints; ``btree_gist`` was installed by ``0002``).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCK_TYPES = ("bol", "commercial_invoice")
APPEND_ONLY_TABLES = ("locked_rates", "gazetted_rates")
PERIOD = "daterange(period_start, period_end, '[]')"


def _uuid_pk() -> sa.Column[sa.Uuid[object]]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _rate() -> sa.Column[sa.Numeric[object]]:
    return sa.Column("rate", sa.Numeric(12, 6), nullable=False)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS fx")
    lock_type = postgresql.ENUM(*LOCK_TYPES, name="lock_type", schema="fx")

    op.create_table(
        "spot_rates",
        _uuid_pk(),
        sa.Column("pair", sa.Text(), nullable=False),
        _rate(),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spot_rates")),
        sa.UniqueConstraint("pair", "as_of", name="uq_spot_rates_pair_as_of"),
        sa.CheckConstraint("rate > 0", name=op.f("ck_spot_rates_positive_rate")),
        schema="fx",
    )

    op.create_table(
        "locked_rates",
        _uuid_pk(),
        sa.Column("pair", sa.Text(), nullable=False),
        _rate(),
        sa.Column("lock_date", sa.Date(), nullable=False),
        sa.Column("lock_type", lock_type, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locked_rates")),
        sa.UniqueConstraint(
            "pair", "lock_type", "lock_date", name="uq_locked_rates_pair_lock_type_lock_date"
        ),
        sa.CheckConstraint("rate > 0", name=op.f("ck_locked_rates_positive_rate")),
        schema="fx",
    )

    op.create_table(
        "gazetted_rates",
        _uuid_pk(),
        sa.Column("pair", sa.Text(), nullable=False),
        _rate(),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("publishing_authority", sa.Text(), nullable=False),
        sa.Column("revision_of", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gazetted_rates")),
        sa.ForeignKeyConstraint(
            ["revision_of"],
            ["fx.gazetted_rates.id"],
            name=op.f("fk_gazetted_rates_revision_of_gazetted_rates"),
        ),
        sa.CheckConstraint("rate > 0", name=op.f("ck_gazetted_rates_positive_rate")),
        sa.CheckConstraint(
            "period_end >= period_start", name=op.f("ck_gazetted_rates_valid_period")
        ),
        postgresql.ExcludeConstraint(
            ("pair", "="),
            (sa.text(PERIOD), "&&"),
            using="gist",
            where=sa.text("revision_of IS NULL"),
            name="ex_gazetted_rates_no_overlap",
        ),
        postgresql.ExcludeConstraint(
            ("revision_of", "="),
            (sa.text(PERIOD), "&&"),
            using="gist",
            where=sa.text("revision_of IS NOT NULL"),
            name="ex_gazetted_rates_revision_no_overlap",
        ),
        schema="fx",
    )

    # SQLSTATE 23001 (restrict_violation) surfaces as an IntegrityError in SQLAlchemy.
    op.execute("""
        CREATE FUNCTION fx.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'fx.% rows are immutable: % rejected', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """)
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON fx.{table} "
            "FOR EACH ROW EXECUTE FUNCTION fx.reject_mutation()"
        )


def downgrade() -> None:
    op.drop_table("gazetted_rates", schema="fx")  # drops its trigger with it
    op.drop_table("locked_rates", schema="fx")
    op.drop_table("spot_rates", schema="fx")
    op.execute("DROP FUNCTION IF EXISTS fx.reject_mutation()")
    postgresql.ENUM(name="lock_type", schema="fx").drop(op.get_bind())
    op.execute("DROP SCHEMA IF EXISTS fx")

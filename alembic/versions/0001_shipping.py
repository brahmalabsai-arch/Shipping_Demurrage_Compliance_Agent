"""Phase 1 — shipping records (shipments, documents) and the audit trail.

``audit.events`` is introduced here rather than in Phase 12 because conventions §3 require every
mutating service function — starting with Phase 1's — to write its audit row in the same
transaction.

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHIPMENT_STATUSES = (
    "quoted",
    "booked",
    "in_transit",
    "docked",
    "under_review",
    "cleared",
    "held",
    "closed",
    "cancelled",
)
DOC_TYPES = ("invoice", "packing_list", "bill_of_lading", "certificate", "other")


def _uuid_pk() -> sa.Column[sa.Uuid[object]]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS shipping")
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")

    shipment_status = postgresql.ENUM(*SHIPMENT_STATUSES, name="shipment_status", schema="shipping")
    doc_type = postgresql.ENUM(*DOC_TYPES, name="doc_type", schema="shipping")

    op.create_table(
        "shipments",
        _uuid_pk(),
        sa.Column("container_id", sa.Text(), nullable=False),
        sa.Column("status", shipment_status, nullable=False),
        sa.Column("cargo_type", sa.Text(), nullable=False),
        sa.Column("hs_code", sa.Text(), nullable=False),
        sa.Column("declared_qty", sa.Numeric(), nullable=False),
        sa.Column("declared_value", sa.Numeric(), nullable=False),
        sa.Column("declared_currency", sa.CHAR(3), nullable=False),
        sa.Column("origin_port", sa.Text(), nullable=False),
        sa.Column("destination_port", sa.Text(), nullable=False),
        sa.Column("etd", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_date", sa.Date(), nullable=True),
        sa.Column("quote_validity_days", sa.Integer(), nullable=True),
        sa.Column("quote_expiry_date", sa.Date(), nullable=True),
        sa.Column("bol_date", sa.Date(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
        sa.UniqueConstraint("container_id", name=op.f("uq_shipments_container_id")),
        schema="shipping",
    )

    op.create_table(
        "documents",
        _uuid_pk(),
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("doc_type", doc_type, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(), nullable=True),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("issued_date", sa.Date(), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipping.shipments.id"],
            name=op.f("fk_documents_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["shipping.documents.id"],
            name=op.f("fk_documents_superseded_by_documents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        schema="shipping",
    )
    op.create_index(
        op.f("ix_shipping_documents_shipment_id"),
        "documents",
        ["shipment_id"],
        unique=False,
        schema="shipping",
    )

    op.create_table(
        "events",
        _uuid_pk(),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        schema="audit",
    )
    op.create_index(
        op.f("ix_audit_events_entity_id"), "events", ["entity_id"], unique=False, schema="audit"
    )
    op.create_index(
        op.f("ix_audit_events_created_at"), "events", ["created_at"], unique=False, schema="audit"
    )


def downgrade() -> None:
    op.drop_table("events", schema="audit")
    op.drop_table("documents", schema="shipping")
    op.drop_table("shipments", schema="shipping")
    postgresql.ENUM(name="doc_type", schema="shipping").drop(op.get_bind())
    postgresql.ENUM(name="shipment_status", schema="shipping").drop(op.get_bind())
    op.execute("DROP SCHEMA IF EXISTS audit")
    op.execute("DROP SCHEMA IF EXISTS shipping")

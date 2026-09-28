"""Phase 7 — the container type of each shipment (``shipping.shipments.container_type``).

Demurrage is tariffed per container type (``demurrage.port_rates``), so the Phase 8 agent must
know it for every shipment; it is required from now on (never assumed by an agent). Rows that
exist before this migration get ``40ft`` — set the real type on the Shipping Ops page. TEXT
checked against the ``ContainerType`` values, like ``port_rates.container_type``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KNOWN_TYPES = "container_type IN ('20ft', '40ft', '40hc', '20rf', '40rf')"


def upgrade() -> None:
    op.add_column(
        "shipments",
        sa.Column("container_type", sa.Text(), nullable=False, server_default="40ft"),
        schema="shipping",
    )
    # The default only backfills existing rows; new shipments must state their type.
    op.alter_column("shipments", "container_type", server_default=None, schema="shipping")
    op.create_check_constraint(
        op.f("ck_shipments_known_container_type"), "shipments", KNOWN_TYPES, schema="shipping"
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_shipments_known_container_type"), "shipments", type_="check", schema="shipping"
    )
    op.drop_column("shipments", "container_type", schema="shipping")

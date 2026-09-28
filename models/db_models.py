"""SQLAlchemy ORM models — the canonical schema for persistence only.

Rows never cross a service-layer boundary; services convert them to the Pydantic ``*Record``
models in ``models/schemas.py`` first (conventions §1). Tables are added phase by phase, each in
its own Postgres schema (``shipping``, ``demurrage``, ``fx``, ``compliance``, ``bulletin``,
``audit``).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column

from models.enums import DocType, LockType, RuleStatus, ShipmentStatus

# Deterministic constraint names so Alembic autogenerate produces stable, reviewable migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

DOMAIN_SCHEMAS = ("shipping", "demurrage", "fx", "compliance", "bulletin", "audit")


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model; its metadata is Alembic's target."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC — the only clock used for stored timestamps (conventions §3)."""
    return datetime.now(UTC)


def _pg_enum(enum_cls: type[StrEnum], name: str, schema: str) -> Enum:
    # Store the lowercase .value (e.g. 'in_transit'), not the member name.
    return Enum(
        enum_cls,
        name=name,
        schema=schema,
        values_callable=lambda members: [m.value for m in members],
    )


def _uuid_pk() -> MappedColumn[uuid.UUID]:
    return mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


# --------------------------------------------------------------------------- shipping


class Shipment(Base):
    """A container shipment and its lifecycle dates (architecture §3.1)."""

    __tablename__ = "shipments"
    __table_args__ = (UniqueConstraint("container_id"), {"schema": "shipping"})

    id: Mapped[uuid.UUID] = _uuid_pk()
    container_id: Mapped[str] = mapped_column(Text)
    status: Mapped[ShipmentStatus] = mapped_column(
        _pg_enum(ShipmentStatus, "shipment_status", "shipping")
    )
    cargo_type: Mapped[str] = mapped_column(Text)
    hs_code: Mapped[str] = mapped_column(Text)
    declared_qty: Mapped[Decimal] = mapped_column(Numeric)
    declared_value: Mapped[Decimal] = mapped_column(Numeric)
    declared_currency: Mapped[str] = mapped_column(CHAR(3))
    origin_port: Mapped[str] = mapped_column(Text)
    destination_port: Mapped[str] = mapped_column(Text)
    etd: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quote_date: Mapped[date | None] = mapped_column(Date)
    quote_validity_days: Mapped[int | None] = mapped_column(Integer)
    quote_expiry_date: Mapped[date | None] = mapped_column(Date)
    bol_date: Mapped[date | None] = mapped_column(Date)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )


class Document(Base):
    """A shipment document version; amendments chain via ``superseded_by`` (architecture §3.2)."""

    __tablename__ = "documents"
    __table_args__ = ({"schema": "shipping"},)

    id: Mapped[uuid.UUID] = _uuid_pk()
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipping.shipments.id"), index=True)
    doc_type: Mapped[DocType] = mapped_column(_pg_enum(DocType, "doc_type", "shipping"))
    version: Mapped[int] = mapped_column(Integer)
    qty: Mapped[Decimal | None] = mapped_column(Numeric)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    issued_date: Mapped[date] = mapped_column(Date)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shipping.documents.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


# --------------------------------------------------------------------------- demurrage


class PortRate(Base):
    """A port's demurrage tariff for one container type over one validity period (arch §3.3).

    Periods for the same (port_code, container_type) can never overlap — enforced by a GiST
    exclusion constraint, so a lookup for any date matches at most one row. ``effective_to`` is
    inclusive; NULL means open-ended. Revisions start a new period instead of overwriting.
    """

    __tablename__ = "port_rates"
    __table_args__ = (
        CheckConstraint("free_days >= 0 AND daily_rate >= 0", name="non_negative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="valid_period"
        ),
        ExcludeConstraint(
            ("port_code", "="),
            ("container_type", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            using="gist",
            name="ex_port_rates_no_overlap",
        ),
        {"schema": "demurrage"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    port_code: Mapped[str] = mapped_column(Text)
    container_type: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(CHAR(3))
    free_days: Mapped[int] = mapped_column(Integer)
    daily_rate: Mapped[Decimal] = mapped_column(Numeric)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)


# --------------------------------------------------------------------------- fx

# Rates are "1 unit of the base currency = rate units of the quote currency" for pair 'BASE/QUOTE'.
FX_RATE = Numeric(12, 6)


class SpotRate(Base):
    """A market spot observation, used only to price quotations (architecture §3.4).

    One observation per (pair, as_of); correcting a mistyped observation edits it in place.
    """

    __tablename__ = "spot_rates"
    __table_args__ = (
        UniqueConstraint("pair", "as_of", name="uq_spot_rates_pair_as_of"),
        CheckConstraint("rate > 0", name="positive_rate"),
        {"schema": "fx"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    pair: Mapped[str] = mapped_column(Text)
    rate: Mapped[Decimal] = mapped_column(FX_RATE)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text)


class LockedRate(Base):
    """A rate locked on a bill-of-lading or commercial-invoice date (architecture §3.5).

    Immutable once written: at most one row per (pair, lock_type, lock_date), and a trigger
    created in migration ``0003`` rejects any UPDATE or DELETE, even bypassing the service.
    """

    __tablename__ = "locked_rates"
    __table_args__ = (
        UniqueConstraint(
            "pair", "lock_type", "lock_date", name="uq_locked_rates_pair_lock_type_lock_date"
        ),
        CheckConstraint("rate > 0", name="positive_rate"),
        {"schema": "fx"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    pair: Mapped[str] = mapped_column(Text)
    rate: Mapped[Decimal] = mapped_column(FX_RATE)
    lock_date: Mapped[date] = mapped_column(Date)
    lock_type: Mapped[LockType] = mapped_column(_pg_enum(LockType, "lock_type", "fx"))


class GazettedRate(Base):
    """A customs-notified rate for a fixed period, used for port charges (architecture §3.6).

    Original notifications (``revision_of`` NULL) for one pair never overlap. A mid-period
    correction is a new row whose ``revision_of`` points at the row it corrects and whose period
    lies within that row's period; revisions of the same row never overlap each other, so the
    deepest revision covering a date is unambiguous. Rows are append-only — the same trigger as
    ``locked_rates`` rejects UPDATE and DELETE.
    """

    __tablename__ = "gazetted_rates"
    __table_args__ = (
        CheckConstraint("rate > 0", name="positive_rate"),
        CheckConstraint("period_end >= period_start", name="valid_period"),
        ExcludeConstraint(
            ("pair", "="),
            (text("daterange(period_start, period_end, '[]')"), "&&"),
            using="gist",
            where=text("revision_of IS NULL"),
            name="ex_gazetted_rates_no_overlap",
        ),
        ExcludeConstraint(
            ("revision_of", "="),
            (text("daterange(period_start, period_end, '[]')"), "&&"),
            using="gist",
            where=text("revision_of IS NOT NULL"),
            name="ex_gazetted_rates_revision_no_overlap",
        ),
        {"schema": "fx"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    pair: Mapped[str] = mapped_column(Text)
    rate: Mapped[Decimal] = mapped_column(FX_RATE)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    publishing_authority: Mapped[str] = mapped_column(Text)
    revision_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fx.gazetted_rates.id"))


# --------------------------------------------------------------------------- bulletin


class BulletinPublication(Base):
    """The simulated external customs bulletin as currently published (one row only).

    Edited from the Customs Bulletin dashboard. The system never reads it directly for change
    detection: only the scheduled scan copies it into ``snapshots`` (edge cases, Phase 6,
    Option A). ``singleton`` (always true, unique) keeps the table to a single row.
    """

    __tablename__ = "publication"
    __table_args__ = (
        UniqueConstraint("singleton"),
        CheckConstraint("singleton", name="single_row"),
        {"schema": "bulletin"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    singleton: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )
    updated_by: Mapped[str] = mapped_column(Text)


class BulletinSnapshot(Base):
    """What the bulletin said at one scheduled scan (architecture §3.8). Append-only.

    ``seq`` orders snapshots unambiguously even when two scans share a timestamp.
    """

    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("seq"), {"schema": "bulletin"})

    id: Mapped[uuid.UUID] = _uuid_pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    content: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class ChangeLogEntry(Base):
    """A detected difference between two consecutive snapshots (architecture §3.8).

    At most one entry per snapshot pair (unique constraint) — this is what makes detection
    idempotent. Only ``emailed_at`` may change after insert (trigger in migration ``0004``).
    """

    __tablename__ = "change_log"
    __table_args__ = (
        UniqueConstraint(
            "prior_snapshot_id", "new_snapshot_id", name="uq_change_log_prior_new_snapshot"
        ),
        CheckConstraint("prior_snapshot_id <> new_snapshot_id", name="distinct_snapshots"),
        {"schema": "bulletin"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    prior_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bulletin.snapshots.id"))
    new_snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bulletin.snapshots.id"))
    diff_summary: Mapped[str] = mapped_column(Text)
    is_substantive: Mapped[bool] = mapped_column(Boolean)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- compliance


class ComplianceRule(Base):
    """One version of a compliance rule (architecture §3.7).

    Every row is created ``pending``; ``compliance_service.decide`` is the only code path that
    sets ``approved``/``rejected``. A trigger created in migration ``0004`` enforces the same
    lifecycle in Postgres: inserts must be pending and undecided, a decision happens once and
    names its reviewer (a rejection also its reason — ``decision_note``, migration ``0006``),
    only an approved row can be superseded, the rule text never changes and rows are never
    deleted. At most one approved, unsuperseded version per ``rule_key``.
    """

    __tablename__ = "rules"
    __table_args__ = (
        UniqueConstraint("rule_key", "version", name="uq_rules_rule_key_version"),
        CheckConstraint("version >= 1", name="positive_version"),
        Index(
            "uq_rules_current_approved",
            "rule_key",
            unique=True,
            postgresql_where=text("status = 'approved' AND superseded_by IS NULL"),
        ),
        {"schema": "compliance"},
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[RuleStatus] = mapped_column(_pg_enum(RuleStatus, "rule_status", "compliance"))
    content: Mapped[str] = mapped_column(Text)
    hs_codes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    effective_date: Mapped[date] = mapped_column(Date)
    change_summary: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("compliance.rules.id"))
    source_diff_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bulletin.change_log.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The reviewer's reason — required for a rejection (trigger, migration 0006).
    decision_note: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- audit


class AuditEvent(Base):
    """One agent action or human decision; written in the same transaction as the change."""

    __tablename__ = "events"
    __table_args__ = ({"schema": "audit"},)

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True
    )

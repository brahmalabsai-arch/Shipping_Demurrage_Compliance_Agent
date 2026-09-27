"""SQLAlchemy ORM models — the canonical schema for persistence only.

Rows never cross a service-layer boundary; services convert them to the Pydantic ``*Record``
models in ``models/schemas.py`` first (conventions §1). Tables are added phase by phase, each in
its own Postgres schema (``shipping``, ``demurrage``, ``fx``, ``compliance``, ``bulletin``,
``audit``).
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

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

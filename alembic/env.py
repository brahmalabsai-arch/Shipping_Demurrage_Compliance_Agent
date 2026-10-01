"""Alembic migration environment.

The database URL comes from ``config.Settings`` (never from alembic.ini), and the target metadata
is ``models.db_models.Base.metadata``. Tables live in per-domain Postgres schemas, so
``include_schemas=True`` lets autogenerate see all of them; each phase's migration creates the
schema it introduces. The ``alembic_version`` table stays in ``public``. The ``checkpoints``
schema (Phase 11) holds LangGraph's checkpoint tables, which the library creates and migrates
itself (``PostgresSaver.setup()``) — autogenerate/``alembic check`` ignore it.
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from config.settings import get_settings
from models.db_models import CHECKPOINT_SCHEMA, Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Leave the library-managed checkpoint schema out of comparisons."""
    return not (type_ == "schema" and name == CHECKPOINT_SCHEMA)


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=get_settings().database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_with(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the live database configured in ``.env``.

    A caller (the pytest harness) may instead hand over an open connection via
    ``config.attributes["connection"]`` to migrate a different database, e.g. the test DB.
    """
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run_with(supplied)
        return

    connectable = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_with(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

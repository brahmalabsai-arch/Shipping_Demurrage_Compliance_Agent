"""Phase 0 exit check: the data stores are usable.

Postgres is the local native install (set up once with scripts/setup_local_db.py); its check is
marked ``db``. ChromaDB is embedded, so its check needs no server.
"""

from pathlib import Path

import chromadb
import pytest
from sqlalchemy import create_engine, text

from config.settings import get_settings


@pytest.mark.db
def test_postgres_accepts_connections_with_utc_sessions() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            version_num: str = conn.execute(text("SHOW server_version_num")).scalar_one()
            timezone: str = conn.execute(text("SHOW TimeZone")).scalar_one()
    finally:
        engine.dispose()

    assert int(version_num) >= 150000, "spec requires PostgreSQL 15+"
    assert timezone in {"UTC", "Etc/UTC"}


def test_embedded_chromadb_persists_to_disk(tmp_path: Path) -> None:
    chroma_dir = tmp_path / "chroma"

    chromadb.PersistentClient(path=str(chroma_dir)).get_or_create_collection("smoke")
    reopened = chromadb.PersistentClient(path=str(chroma_dir))

    assert reopened.heartbeat() > 0
    assert [c.name for c in reopened.list_collections()] == ["smoke"]

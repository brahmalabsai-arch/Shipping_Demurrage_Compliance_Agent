"""The Chroma collection that holds the searchable rulebook (embedded, at ``CHROMA_PATH``).

One entry per ``rule_key`` — the approved version *in force*: approving a new version replaces
its predecessor's entry (same id), so a superseded, pending or rejected text is never a
candidate. Vectors are always supplied by :mod:`rag.models` (no Chroma embedding function), so
the index and the queries use the same model.
"""

from pathlib import Path
from typing import Any

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection

from config.settings import get_settings

COLLECTION = "compliance_rules"

_clients: dict[Path, ClientAPI] = {}


def _client(path: Path) -> ClientAPI:
    if path not in _clients:
        path.mkdir(parents=True, exist_ok=True)
        _clients[path] = chromadb.PersistentClient(path=str(path))
    return _clients[path]


def collection() -> Collection:
    """The rulebook collection at the configured ``CHROMA_PATH`` (created if missing)."""
    options: dict[str, Any] = {"embedding_function": None}
    return _client(get_settings().chroma_path).get_or_create_collection(COLLECTION, **options)

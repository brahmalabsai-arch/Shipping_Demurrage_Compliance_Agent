"""Pydantic v2 models — the canonical schema for MCP tool I/O, LangGraph state and FastAPI bodies.

Domain objects are named ``<Noun>Record`` (e.g. ``ShipmentRecord``) to distinguish them from the
SQLAlchemy ``<Noun>`` ORM classes (conventions §2). Records are added phase by phase.
"""

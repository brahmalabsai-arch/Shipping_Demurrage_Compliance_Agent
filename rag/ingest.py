"""Put approved rules into the searchable index — and nothing else.

``index_rule`` refuses anything that is not the approved version in force (``status='approved'``
and not superseded): pending or rejected content is never embedded, so it cannot be retrieved
even by accident (implementation plan Phase 9). It is called by ``compliance_service`` only
*after* the approving transaction commits, with the rule re-read from the database; the entry
id is the ``rule_key``, so the new version replaces its predecessor's entry.

``sync_index`` makes the collection exactly the given rulebook in force (upsert every rule,
drop every other entry) — the backfill for rules approved before Phase 9, and the repair if an
after-commit indexing ever failed (``python -m scripts.index_rulebook``).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from models.enums import RuleStatus
from models.schemas import ComplianceRuleRecord
from rag import models, store
from services.errors import RuleNotIndexableError

logger = structlog.get_logger("rag")


def document_text(rule: ComplianceRuleRecord) -> str:
    """What is embedded and searched: the rule key (its words help keyword search) + the text."""
    return f"{rule.rule_key}\n{rule.content}"


def _metadata(rule: ComplianceRuleRecord) -> dict[str, str | int]:
    return {
        "rule_id": str(rule.id),
        "rule_key": rule.rule_key,
        "version": rule.version,
        "hs_codes": ",".join(rule.hs_codes),  # "" = all goods
        "effective_date": rule.effective_date.isoformat(),
    }


def _check_in_force(rule: ComplianceRuleRecord) -> None:
    if rule.status is not RuleStatus.APPROVED or rule.superseded_by is not None:
        state = "superseded" if rule.superseded_by is not None else rule.status.value
        raise RuleNotIndexableError(
            f"{rule.rule_key} v{rule.version} is {state}; only the approved version in force is "
            "indexed"
        )


def index_rules(rules: Sequence[ComplianceRuleRecord]) -> None:
    """Embed and upsert approved rules in force (one entry per rule_key)."""
    for rule in rules:
        _check_in_force(rule)
    if not rules:
        return
    texts = [document_text(r) for r in rules]
    store.collection().upsert(
        ids=[r.rule_key for r in rules],
        documents=texts,
        embeddings=models.get_embedder().embed(texts),  # type: ignore[arg-type]
        metadatas=[_metadata(r) for r in rules],
    )
    logger.info("rag.indexed", rules=[f"{r.rule_key} v{r.version}" for r in rules])


def index_rule(rule: ComplianceRuleRecord) -> None:
    """Embed one approved rule in force, replacing the entry of its previous version."""
    index_rules([rule])


@dataclass(frozen=True)
class SyncReport:
    """What ``sync_index`` changed."""

    indexed: list[str]  # "<rule_key> v<version>"
    removed: list[str]  # rule_keys dropped from the index


def sync_index(rules_in_force: Sequence[ComplianceRuleRecord]) -> SyncReport:
    """Make the index exactly ``rules_in_force`` (``compliance_service.list_approved``)."""
    collection = store.collection()
    wanted = {r.rule_key for r in rules_in_force}
    stale = [key for key in collection.get(include=[])["ids"] if key not in wanted]
    if stale:
        collection.delete(ids=stale)
    index_rules(rules_in_force)
    return SyncReport([f"{r.rule_key} v{r.version}" for r in rules_in_force], sorted(stale))

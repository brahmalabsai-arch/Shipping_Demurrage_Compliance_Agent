"""Opt-in RAGAS report for the compliance retriever (LLM-judged; not part of pytest).

The RAGAS package itself would downgrade openai / langchain-openai / tenacity in ``.venv``, so
its two retrieval metrics are re-implemented here with the published formulas, judged by the
reasoning-tier LLM (Groq ``openai/gpt-oss-120b`` by default):

* **Context precision@K** — for the ranked retrieved rules c1..cK the judge says whether each is
  useful for the reference answer (v_k = 1/0); score = sum_k(precision@k * v_k) / sum_k(v_k),
  with precision@k = (v_1 + ... + v_k) / k. Rewards putting the useful rule first.
* **Context recall** — the reference answer is split into claims (sentences); the judge says
  whether each is supported by the retrieved rules; score = supported / total claims.

The reference answer of a golden question is the text of the rule that answers it. The index is
built in a temporary directory from the baseline rulebook (``tests/fixtures/compliance.py``),
so the report is reproducible and never touches the dev database or ``./chroma_data``. The
deterministic gate (hit@1 / hit@3 / MRR) is ``pytest -m models``.

    .venv\\Scripts\\python.exe -m scripts.evaluate_rag              # needs GROQ_API_KEY
    .venv\\Scripts\\python.exe -m scripts.evaluate_rag --out rag_report.json
"""

import argparse
import json
import re
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from agents import llm
from config.settings import get_settings
from models.enums import RuleStatus
from models.schemas import ComplianceRuleRecord
from rag import ingest, retriever
from services.bulletin_service import parse_sections
from tests.fixtures.compliance import BASELINE_BULLETIN
from tests.fixtures.rag import GOLDEN_SET

K = 3
JUDGE_SYSTEM = (
    "You are a strict evaluator of a retrieval system for customs rules. Answer only with a "
    'JSON object, no prose, no markdown: {"verdict": 1} for yes or {"verdict": 0} for no.'
)


# --------------------------------------------------------------------------- the formulas


def context_precision(verdicts: Sequence[int]) -> float:
    """RAGAS context precision@K from ranked usefulness verdicts (1 useful, 0 not)."""
    useful = sum(verdicts)
    if useful == 0:
        return 0.0
    total, hits = 0.0, 0
    for k, verdict in enumerate(verdicts, start=1):
        hits += verdict
        total += (hits / k) * verdict
    return total / useful


def context_recall(supported: Sequence[int]) -> float:
    """RAGAS context recall: share of reference claims supported by the retrieved context."""
    return sum(supported) / len(supported) if supported else 0.0


def claims(reference: str) -> list[str]:
    """The reference answer split into sentence-level claims."""
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", reference) if s.strip()]


# --------------------------------------------------------------------------- the judge


def _invoke(model: BaseChatModel, prompt: str, attempts: int = 6) -> str:
    """One judge call; waits and retries on provider rate limits (Groq's free tier allows
    8k tokens/minute, and a report makes ~50 calls)."""
    for attempt in range(1, attempts + 1):
        try:
            reply = model.invoke([SystemMessage(JUDGE_SYSTEM), HumanMessage(prompt)])
            return reply.content if isinstance(reply.content, str) else ""
        except Exception as exc:
            limited = "rate limit" in str(exc).lower() or "429" in str(exc)
            if not limited or attempt == attempts:
                raise
            wait = 10 * attempt
            print(f"  rate limited; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _verdict(model: BaseChatModel, prompt: str) -> int:
    text = _invoke(model, prompt)
    match = re.search(r'"verdict"\s*:\s*([01])', text)
    if match is None:
        raise ValueError(f"unparseable judge reply: {text[:200]!r}")
    return int(match.group(1))


def judge_precision(model: BaseChatModel, question: str, reference: str, context: str) -> int:
    return _verdict(model, (
        f"Question: {question}\n\nReference answer: {reference}\n\nRetrieved rule:\n{context}\n\n"
        "Was this retrieved rule useful in arriving at the reference answer to the question?"
    ))  # fmt: skip


def judge_claim(model: BaseChatModel, claim: str, contexts: Sequence[str]) -> int:
    joined = "\n\n".join(contexts) or "(nothing retrieved)"
    return _verdict(model, (
        f"Retrieved rules:\n{joined}\n\nStatement: {claim}\n\n"
        "Can this statement be attributed to (is it supported by) the retrieved rules?"
    ))  # fmt: skip


# --------------------------------------------------------------------------- the run


def baseline_rulebook() -> list[ComplianceRuleRecord]:
    """The six baseline rules as approved v1 records (what the seed approves)."""
    now = datetime.now(UTC)
    return [
        ComplianceRuleRecord(
            id=uuid4(), rule_key=s.rule_key, version=1, status=RuleStatus.APPROVED,
            content=s.text, hs_codes=list(s.hs_codes), effective_date=s.effective_date,
            change_summary=None, superseded_by=None, source_diff_id=None, created_at=now,
            decided_by="evaluate_rag", decided_at=now, decision_note=None,
        )
        for s in parse_sections(BASELINE_BULLETIN)
    ]  # fmt: skip


def evaluate(model: BaseChatModel) -> dict[str, Any]:
    """Index the baseline rulebook in a temp dir, retrieve for each golden question, judge."""
    settings = get_settings()
    settings.chroma_path = Path(tempfile.mkdtemp(prefix="rag_eval_")) / "chroma"
    rules = baseline_rulebook()
    ingest.index_rules(rules)
    reference = {r.rule_key: r.content for r in rules}

    rows: list[dict[str, Any]] = []
    for q in GOLDEN_SET:
        hits = retriever.search(q.question, q.hs_code, limit=K)
        contexts = [h.text for h in hits]
        ref = reference[q.expected]
        precision_verdicts = [judge_precision(model, q.question, ref, c) for c in contexts]
        recall_flags = [judge_claim(model, claim, contexts) for claim in claims(ref)]
        rows.append({
            "question": q.question, "expected": q.expected,
            "retrieved": [h.rule_key for h in hits],
            "context_precision": round(context_precision(precision_verdicts), 4),
            "context_recall": round(context_recall(recall_flags), 4),
            "precision_verdicts": precision_verdicts, "recall_flags": recall_flags,
        })  # fmt: skip
        print(f"  CP={rows[-1]['context_precision']:.2f} CR={rows[-1]['context_recall']:.2f}  "
              f"{q.question}", file=sys.stderr)  # fmt: skip
    n = len(rows)
    return {
        "judge": getattr(model, "model_name", None) or getattr(model, "model", None),
        "embedding_model": settings.embedding_model,
        "reranker_model": settings.reranker_model,
        "k": K,
        "questions": n,
        "context_precision": round(sum(r["context_precision"] for r in rows) / n, 4),
        "context_recall": round(sum(r["context_recall"] for r in rows) / n, 4),
        "hit_at_1": round(sum(r["retrieved"][:1] == [r["expected"]] for r in rows) / n, 4),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.evaluate_rag")
    parser.add_argument("--out", type=Path, help="also write the full report as JSON here")
    args = parser.parse_args(argv)
    model = llm.reasoning_model()
    if model is None:
        sys.exit("No LLM configured: set GROQ_API_KEY (or LLM_PROVIDER=openai + OPENAI_API_KEY).")
    report = evaluate(model)
    summary = {k: v for k, v in report.items() if k != "rows"}
    print(json.dumps(summary, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"full report: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

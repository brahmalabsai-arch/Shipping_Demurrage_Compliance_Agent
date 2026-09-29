"""The RAGAS formulas re-implemented in scripts/evaluate_rag.py (the LLM judge is not tested)."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from scripts import evaluate_rag as ev


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([1, 0, 0], 1.0),  # the useful rule ranked first
        ([0, 1, 0], 0.5),  # ranked second: precision@2 = 1/2
        ([1, 0, 1], (1 / 1 + 2 / 3) / 2),
        ([0, 0, 0], 0.0),
        ([], 0.0),
    ],
)
def test_context_precision_rewards_useful_rules_ranked_first(
    verdicts: list[int], expected: float
) -> None:
    assert ev.context_precision(verdicts) == pytest.approx(expected)


def test_context_recall_is_the_share_of_supported_claims() -> None:
    assert ev.context_recall([1, 1, 0, 1]) == 0.75
    assert ev.context_recall([]) == 0.0


def test_reference_answers_split_into_claims() -> None:
    text = (
        "A bill of entry cannot be assessed without it. Goods remain uncleared; demurrage accrues."
    )

    assert ev.claims(text) == [
        "A bill of entry cannot be assessed without it.",
        "Goods remain uncleared;",
        "demurrage accrues.",
    ]


def test_judge_reply_must_be_a_json_verdict() -> None:
    assert ev._verdict(FakeListChatModel(responses=['{"verdict": 1}']), "q") == 1
    with pytest.raises(ValueError, match="unparseable"):
        ev._verdict(FakeListChatModel(responses=["Yes, it is useful."]), "q")


def test_baseline_rulebook_is_the_six_seeded_rules() -> None:
    rules = ev.baseline_rulebook()

    assert len(rules) == 6
    assert {r.version for r in rules} == {1}

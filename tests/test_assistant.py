"""The support assistant's retrieval, and the eval's deterministic checks."""

import pytest

from llm_bonsai.assistant import PROMPTS, Variant, messages, retrieve, stem
from llm_bonsai.probe.evaluation import CASES, mentions, parse_rating


def test_retrieve_ranks_by_shared_words() -> None:
    assert retrieve("Which payment methods do you accept? PayPal?")[0].id == "payment-methods"
    assert [s.id for s in retrieve("When will my refund be approved?", k=1)] == ["refund-time"]


def test_retrieve_returns_nothing_without_overlap() -> None:
    assert retrieve("Tell me a joke") == []


def test_prompt_versions_are_compared_by_role_and_content() -> None:
    stored = [{"type": "message", **m} for m in PROMPTS[Variant.strict]]
    assert messages(stored) == messages(PROMPTS[Variant.strict])
    assert messages(stored) != messages(PROMPTS[Variant.baseline])


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Refunds are possible within 30 days.", True),
        ("Within 30 DAYS of purchase", True),
        ("Within 130 days", False),
        ("Thirty days", False),
    ],
)
def test_mentions_matches_whole_words_ignoring_case(answer: str, expected: bool) -> None:
    assert mentions(answer, ["30 days"]) is expected


def test_every_case_mentions_its_own_reference_answer() -> None:
    for case in CASES:
        assert mentions(case.answer, list(case.must_mention)), case.id


@pytest.mark.parametrize(
    ("reply", "rating"),
    [("4", 4), (" 5.", 5), ("Score: 3", 3), ("5 = same facts, 3 = partly", None), ("7", None), ("great", None)],
)
def test_parse_rating_needs_exactly_one_number_from_1_to_5(reply: str, rating: int | None) -> None:
    assert parse_rating(reply) == rating


def test_retrieval_matches_plurals_and_past_tense() -> None:
    assert [s.id for s in retrieve("How soon do you answer a chargeback?", k=1)] == ["chargeback"]
    assert stem("business") == "business" and stem("shipped") == "shipp" and stem("days") == "day"

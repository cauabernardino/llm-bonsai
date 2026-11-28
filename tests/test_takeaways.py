"""The "What you just saw" bullets: each claim must follow from what the run measured."""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from llm_bonsai.catalog import CHAT_SMALL
from llm_bonsai.models import Attempt, Observation
from llm_bonsai.probe import app, cache, chaos, evaluation, fallback, limits, load, replicas
from llm_bonsai.probe.load import Level


def reply(elapsed: float, hit: bool, cost: float, answer: str = "haiku") -> cache.Reply:
    headers = {"x-litellm-response-cost": str(cost)} | ({"x-litellm-cache-key": "k"} if hit else {})
    return cache.Reply(elapsed, headers, answer)


def test_cache_hit_is_reported_with_its_speedup() -> None:
    bullets = cache.takeaways(
        reply(1.2, False, 0.01), reply(0.012, True, 0), reply(1.0, False, 0.01), reply(1.1, False, 0.01)
    )
    assert "~100× faster" in bullets[0] and "word for word" in bullets[0]
    assert "missed" in bullets[1]
    assert "didn't opt in" in bullets[2]


def test_cache_miss_on_the_repeat_says_the_cache_is_broken() -> None:
    bullets = cache.takeaways(
        reply(1.2, False, 0.01), reply(1.1, False, 0.01), reply(1.0, False, 0.01), reply(1, False, 0.01)
    )
    assert bullets == [bullets[0]] and "isn't working" in bullets[0]


def observation(name: str, type: str = "SPAN", parent: str | None = None, **fields: Any) -> Observation:
    defaults: dict[str, Any] = {
        "id": name,
        "trace_id": "t",
        "parent_id": parent,
        "type": type,
        "name": name,
        "start_time": datetime.now(UTC),
        "level": "DEFAULT",
        "latency": 0.0,
        "model": None,
        "status_message": "",
        "total_tokens": 0,
        "total_cost": 0.0,
        "prompt_name": None,
        "prompt_version": None,
        "metadata": {},
    }
    return Observation(**(defaults | fields))


def generation(level: str, latency: float) -> Observation:
    return observation("litellm_request", "GENERATION", level=level, latency=latency, model="chat-small")


ERROR = generation("ERROR", 0.006)
OK = generation("DEFAULT", 2.4)


def test_fallback_without_langfuse_only_claims_the_headers() -> None:
    bullets = fallback.takeaways({"x-litellm-attempted-fallbacks": "1", "x-litellm-model-group": "chat-small"}, [])
    assert len(bullets) == 1 and "attempted-fallbacks: 1" in bullets[0]


def test_fallback_with_both_attempts_reports_the_failure_time() -> None:
    bullets = fallback.takeaways({"x-litellm-attempted-fallbacks": "1"}, [ERROR, OK])
    assert "failed in 6 ms" in bullets[2]


def test_fallback_that_did_not_happen_is_called_out() -> None:
    assert "no fallback" in fallback.takeaways({}, [])[0]


def budget(crossing: bool, spent: float = 0.0522) -> limits.BudgetResult:
    return limits.BudgetResult(limits.Crossing(1, 0.0096, 0.0522) if crossing else None, 2, 422, spent)


def test_limits_report_the_crossing_and_the_postgres_lag() -> None:
    quick = limits.RateLimitResult(allowed=3, retry_after="60", reset_in=60, spent=0.0096)
    bullets = limits.takeaways(quick, budget(crossing=True), recorded=0.0096)
    assert "3 of 10" in bullets[0] and "retry-after: 60" in bullets[0]
    assert "overspend" in bullets[1]
    assert "HTTP 422" in bullets[2]
    assert "Postgres still showed $0.0096 of $0.0522" in bullets[3]


def test_limits_only_claim_what_happened() -> None:
    quick = limits.RateLimitResult(allowed=10, retry_after=None, reset_in=0, spent=0.01)
    bullets = limits.takeaways(quick, budget(crossing=False), recorded=0.0522)
    assert "wasn't enforced" in bullets[0]
    assert "overspend" not in bullets[1]
    assert len(bullets) == 3  # Postgres had caught up: no lag claim


def level(concurrency: int, rps: float, p50: float, ttft50: float) -> Level:
    return Level(concurrency, 10, 10, rps, p50, p50, ttft50, ttft50, Counter())


SWEEP = [level(1, 0.33, 3.0, 0.1), level(2, 0.47, 4.3, 0.2), level(4, 0.47, 7.5, 4.2), level(8, 0.38, 17.5, 13.0)]


def test_load_explains_queueing_in_ollama() -> None:
    bullets = load.takeaways(SWEEP, "chat-small")
    assert "OLLAMA_NUM_PARALLEL=2" in bullets[0] and "not 2×" in bullets[0]
    assert "stopped growing" in bullets[1] and "Ollama's queue" in bullets[1]


def test_load_on_another_model_does_not_mention_ollama() -> None:
    bullets = load.takeaways(SWEEP, "chat-vllm")
    assert len(bullets) == 1 and "Ollama" not in bullets[0] and "the model server's queue" in bullets[0]


def test_load_that_kept_scaling_is_not_called_flat() -> None:
    scaling = [level(1, 0.3, 3.0, 0.1), level(2, 0.6, 3.1, 0.1), level(4, 1.2, 3.2, 0.1)]
    assert "stopped growing" not in load.takeaways(scaling, "chat-vllm")[0]


def test_measure_uses_wall_time_and_counts_errors() -> None:
    results = [Attempt(0.0, 2.0, 0.5, None), Attempt(2.0, 2.0, 0.7, None), Attempt(4.0, 0.1, None, "HTTP 500")]
    measured = load.measure(1, results)
    assert (measured.attempts, measured.ok) == (3, 2)
    assert abs(measured.rps - 2 / 4.1) < 1e-9
    assert "1 (HTTP 500 ×1)" in load.row(measured)


def test_replicas_report_the_doubled_and_the_shared_limit() -> None:
    bullets = replicas.takeaways(replicas.Outcome(6, "3 + 3"), replicas.Outcome(3, "1 + 2"))
    assert "6 requests (3 + 3 per pod)" in bullets[0]
    assert "the limit held" in bullets[1]


def test_replicas_say_so_when_the_effect_did_not_show() -> None:
    bullets = replicas.takeaways(replicas.Outcome(3, "3 + 0"), replicas.Outcome(5, "3 + 2"))
    assert "Run it again" in bullets[0]
    assert "didn't share a counter" in bullets[1]


def test_chaos_assess_finds_the_outage_and_the_cut_off_requests() -> None:
    results = [
        Attempt(10.0, 2.0, 0.1, None),
        Attempt(14.0, 2.0, None, "RemoteProtocolError"),  # in flight when the pod died at t=15
        Attempt(16.0, 0.0, None, "ConnectError"),
        Attempt(30.0, 0.0, None, "ConnectError"),
        Attempt(35.0, 2.0, 0.1, None),
    ]
    damage = chaos.assess(results, killed_at=15.0)
    assert (damage.failed, damage.cut_off, damage.recovered_at) == (3, 1, 35.0)
    assert damage.error_kinds == ["ConnectError", "RemoteProtocolError"]


def test_chaos_assess_without_recovery() -> None:
    damage = chaos.assess([Attempt(16.0, 0.0, None, "ConnectError")], killed_at=15.0)
    assert damage.recovered_at is None


def app_trace(*, gateway_parent: str | None = "answer", prompt: bool = True) -> list[Observation]:
    trace = [
        observation("support-assistant"),
        observation("retrieval", "RETRIEVER", parent="support-assistant"),
        observation(
            "answer",
            "GENERATION",
            parent="support-assistant",
            prompt_name="support-answer" if prompt else None,
            prompt_version=2 if prompt else None,
        ),
    ]
    if gateway_parent:
        trace.append(
            observation("litellm_request", "GENERATION", parent=gateway_parent, total_tokens=84, total_cost=0.0099)
        )
    return trace


def test_app_trace_with_everything_nested() -> None:
    shape = app.shape(app_trace())
    assert shape.nested and shape.gateway is not None and shape.prompt == "support-answer v2"
    bullets = app.takeaways(shape)
    assert "84 tokens" in bullets[1] and "traceparent" in bullets[1]
    assert "support-answer v2" in bullets[2]


def test_app_trace_without_the_gateway_part() -> None:
    shape = app.shape(app_trace(gateway_parent=None, prompt=False))
    assert shape.gateway is None and shape.prompt is None
    bullets = app.takeaways(shape)
    assert "didn't show up" in bullets[1] and len(bullets) == 2


def test_app_tree_indents_children_under_their_parent() -> None:
    lines = app.tree(app_trace())
    assert [line.split(" (")[0] for line in lines] == [
        "  support-assistant",
        "     retrieval",
        "     answer",
        "        litellm_request",
    ]


def eval_row(keyword: bool, rating: int | None) -> evaluation.Row:
    return evaluation.Row("support-faq-01", "q", "a", keyword, rating, None if rating is None else str(rating))


def test_eval_with_a_constant_judge_that_contradicts_the_keyword_check() -> None:
    rows = [eval_row(True, 5), eval_row(False, 5), eval_row(False, None)]
    bullets = evaluation.takeaways(rows, total=4, judge=True)
    assert "1 of 4 items failed" in bullets[0]
    assert "1 of 3 answers" in bullets[1]
    assert "mean 5.0 over 2 answers, and 1 parse failure (replies" in bullets[2]
    assert "isn't judging" in bullets[3] and "calibration" in bullets[3]


def test_eval_with_a_constant_judge_the_keyword_check_agrees_with() -> None:
    bullets = evaluation.takeaways([eval_row(True, 5), eval_row(True, 5)], total=2, judge=True)
    assert "can't tell a correct judge" in bullets[2]


def test_eval_judge_that_varies_and_disagrees() -> None:
    bullets = evaluation.takeaways([eval_row(True, 1), eval_row(True, 4)], total=2, judge=True)
    assert "contradicted the keyword check on 1 answer " in bullets[2]


def test_eval_without_the_judge_or_any_parsed_rating() -> None:
    assert len(evaluation.takeaways([eval_row(True, None)], total=1, judge=False)) == 1
    bullets = evaluation.takeaways([eval_row(True, None)], total=1, judge=True)
    assert "no judge score" in bullets[1] and len(bullets) == 2


def test_load_says_requests_slowed_down_only_when_they_did() -> None:
    same = load.takeaways([level(1, 0.2, 4.9, 0.3), level(2, 0.4, 4.9, 0.1)], CHAT_SMALL)
    assert "took about as long (p50 4.9s → 4.9s)" in same[0]
    slower = load.takeaways([level(1, 0.33, 3.0, 0.1), level(2, 0.47, 4.3, 0.2)], CHAT_SMALL)
    assert "got slower (p50 3.0s → 4.3s)" in slower[0]

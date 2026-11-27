from datetime import UTC, datetime

import pytest

from llm_bonsai.probe.fallback import describe_failure
from llm_bonsai.probe.limits import DEFAULT_WINDOW_S, seconds_until_reset
from llm_bonsai.probe.load import percentile
from llm_bonsai.probe.replicas import ACCESS_LOG


def test_describe_failure_keeps_host_and_reason() -> None:
    message = (
        "litellm.APIConnectionError: Cannot connect to host does-not-exist.llm.svc.cluster.local:1234 "
        "ssl:default [Name or service not known]"
    )
    assert describe_failure(message) == "does-not-exist.llm.svc.cluster.local:1234 [Name or service not known]"


def test_describe_failure_falls_back_to_the_message() -> None:
    assert describe_failure("timeout") == "timeout"


def test_seconds_until_reset_reads_litellm_429_message() -> None:
    now = datetime(2026, 11, 27, 20, 45, 30, tzinfo=UTC)
    message = "Rate limit exceeded. Limit resets at: 2026-11-27 20:45:56 UTC"
    assert seconds_until_reset(message, now) == 27  # 26s to go, plus a second of margin


def test_seconds_until_reset_defaults_to_a_full_window() -> None:
    assert seconds_until_reset("429 without a reset time", datetime.now(UTC)) == DEFAULT_WINDOW_S


@pytest.mark.parametrize(("values", "expected"), [([], None), ([2.0], 2.0), ([1.0, 2.0, 3.0], 2.0)])
def test_percentile(values: list[float], expected: float | None) -> None:
    assert percentile(values, 50) == expected


def test_access_log_pattern_matches_uvicorn_lines() -> None:
    logs = (
        'INFO:     10.244.0.9:51234 - "POST /v1/chat/completions HTTP/1.1" 200 OK\n'
        'INFO:     10.244.0.9:51236 - "POST /v1/chat/completions HTTP/1.1" 429 Too Many Requests\n'
        'INFO:     10.244.0.1:40000 - "GET /health/liveliness HTTP/1.1" 200 OK\n'
    )
    assert ACCESS_LOG.findall(logs) == ["200", "429"]

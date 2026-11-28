import httpx
import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from llm_bonsai.cli import app, describe
from llm_bonsai.probe.cli import concurrency_levels


def subcommands(*path: str) -> set[str]:
    group = typer.main.get_command(app)
    for name in path:
        assert isinstance(group, TyperGroup)
        group = group.commands[name]
    assert isinstance(group, TyperGroup)
    return set(group.commands)


def test_command_tree() -> None:
    assert subcommands() == {"setup", "probe", "ui", "chat", "teardown"}
    assert subcommands("setup") == {
        "preflight", "cluster", "namespaces", "secrets", "ollama", "postgres", "valkey",
        "cert-manager", "clickhouse-operator", "langfuse", "litellm", "all", "teams", "prometheus",
    }  # fmt: skip
    assert subcommands("probe") == {"smoke", "fallback", "limits", "cache", "replicas", "chaos", "load", "app", "eval"}


def test_teardown_asks_before_deleting() -> None:
    result = CliRunner().invoke(app, ["teardown"], input="n\n")
    assert result.exit_code == 1
    assert "Delete the lab's releases and all their data?" in result.output


def test_concurrency_levels_are_sorted_and_deduplicated() -> None:
    assert concurrency_levels("8,1,2,2") == [1, 2, 8]


@pytest.mark.parametrize("value", ["1,two", "0,2", ""])
def test_concurrency_levels_rejects_bad_input(value: str) -> None:
    with pytest.raises(typer.BadParameter):
        concurrency_levels(value)


def failure(status: int, **body: object) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://127.0.0.1:4000/user/info?key=sk-secret")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("failed", request=request, response=response)


def test_describe_shows_the_gateways_message_and_never_the_query() -> None:
    line = describe(failure(401, error={"message": "Authentication Error, invalid key", "type": "auth_error"}))
    assert line == "GET 127.0.0.1/user/info answered HTTP 401: Authentication Error, invalid key"


def test_describe_falls_back_to_detail_or_text() -> None:
    assert describe(failure(422, detail="Malformed request")).endswith("HTTP 422: Malformed request")
    assert describe(failure(400, detail={"error": "Team not found"})).endswith("HTTP 400: Team not found")
    request = httpx.Request("POST", "http://gateway/key/generate")
    error = httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(502, text="bad\ngateway", request=request)
    )
    assert describe(error).endswith("HTTP 502: bad gateway")

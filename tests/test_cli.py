import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from llm_bonsai.cli import app
from llm_bonsai.probe.cli import concurrency_levels


def subcommands(*path: str) -> set[str]:
    group = typer.main.get_command(app)
    for name in path:
        assert isinstance(group, TyperGroup)
        group = group.commands[name]
    assert isinstance(group, TyperGroup)
    return set(group.commands)


def test_command_tree() -> None:
    assert subcommands() == {"setup", "probe", "ui", "teardown"}
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

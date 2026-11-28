import pytest

from llm_bonsai.chat import Command, Conversation, Reply, parse, split_typed, stats


def test_each_turn_sends_the_system_prompt_the_history_and_the_new_message() -> None:
    conversation = Conversation(system="Be brief.")
    conversation.record("hi", "hello")
    assert conversation.messages("and now?") == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "and now?"},
    ]


def test_no_system_prompt_means_no_system_message() -> None:
    assert Conversation().messages("hi") == [{"role": "user", "content": "hi"}]


def test_stats_show_what_was_measured() -> None:
    assert stats(Reply("x", 2.04, 0.31, 12, interrupted=False)) == "2.0s · first token 0.31s · 12 tokens"
    assert stats(Reply("", 0.5, None, None, interrupted=True)) == "0.5s · stopped"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/exit", Command.EXIT), (" /Quit ", Command.EXIT), ("/q", Command.EXIT), ("exit", Command.EXIT),
        ("QUIT", Command.EXIT), ("/reset", Command.RESET), ("/clear", Command.RESET), ("/help", Command.HELP),
        ("/exti", Command.UNKNOWN), ("/", Command.UNKNOWN),
        ("  hello there ", "hello there"), ("exit the building, how?", "exit the building, how?"), ("", ""),
    ],
)  # fmt: skip
def test_parse_never_sends_a_slash_command_to_the_model(line: str, expected: Command | str) -> None:
    assert parse(line) == expected


def test_typing_during_a_reply_becomes_lines_and_a_partial_line() -> None:
    assert split_typed("", "/exit\r") == (["/exit"], "")
    assert split_typed("", "what about\x7f\x7f\x7f\x7f\x7fwho") == ([], "what who")
    assert split_typed("hel", "lo\nnext") == (["hello"], "next")
    assert split_typed("", "ab\x1b[Dc\x1bOH\x1b[1;5C") == ([], "abc")  # arrow, Home, Ctrl-arrow dropped

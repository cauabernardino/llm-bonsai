"""`llm-bonsai chat`: talk to a model through the gateway, the way any client would.

Every turn is an ordinary gateway request with the support-bot key: it counts against the
team's rate limit and budget, and LiteLLM traces it to Langfuse. The whole conversation is
one Langfuse session, so its turns read in order under Sessions.
"""

import os
import re
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

import openai
from openai.types.chat import ChatCompletionMessageParam
from rich.text import Text

from llm_bonsai import report
from llm_bonsai.catalog import LANGFUSE_PROJECT_ID, LANGFUSE_UI_URL, SUPPORT_BOT
from llm_bonsai.clients.langfuse import run_id
from llm_bonsai.lab import Lab

try:  # POSIX only; without them the chat still works, it just echoes keys typed during a reply
    import readline
    import termios
except ImportError:  # pragma: no cover
    readline = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]


class Command(Enum):
    EXIT = "exit"
    RESET = "reset"
    HELP = "help"
    UNKNOWN = "unknown"


ALIASES = {
    "/exit": Command.EXIT, "/quit": Command.EXIT, "/q": Command.EXIT, "exit": Command.EXIT, "quit": Command.EXIT,
    "/reset": Command.RESET, "/clear": Command.RESET,
    "/help": Command.HELP, "/?": Command.HELP,
}  # fmt: skip
HELP = [
    ("/reset, /clear", "forget the conversation so far"),
    ("/exit, /quit", "leave (Ctrl-D works too)"),
    ("/help", "this list"),
    ("Ctrl-C", "stop a reply that is still streaming"),
]


def parse(line: str) -> Command | str:
    """A command, or the message to send. Anything starting with "/" is a command, never a message."""
    text = line.strip()
    command = ALIASES.get(text.lower())
    if command is not None:
        return command
    return Command.UNKNOWN if text.startswith("/") else text


def split_typed(partial: str, typed: str) -> tuple[list[str], str]:
    """What was typed while a reply streamed: the finished lines, and what's left on the current one.

    The terminal doesn't edit the line while it's in raw mode, so backspaces are applied here, and
    escape sequences (arrow keys, Home, End) are dropped whole.
    """
    lines: list[str] = []
    current = list(partial)
    typed = re.sub(r"\x1b(\[[0-9;]*[ -/]*[@-~]|O.|.)?", "", typed)
    for char in typed:
        if char in "\x7f\b":
            if current:
                current.pop()
        elif char in "\r\n":
            lines.append("".join(current))
            current = []
        elif char.isprintable():
            current.append(char)
    return lines, "".join(current)


class Keyboard:
    """Reads the user's lines. While a reply streams, keys aren't echoed, so they can't land in the
    middle of the reply; what was typed comes back at the next prompt instead."""

    def __init__(self) -> None:
        interactive = termios is not None and sys.stdin.isatty()
        self._fd = sys.stdin.fileno() if interactive else None
        self._lines: list[str] = []
        self._partial = ""

    @contextmanager
    def quiet(self) -> Generator[None]:
        fd = self._fd
        if fd is None:
            yield
            return
        saved = termios.tcgetattr(fd)
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ECHO | termios.ICANON)  # no echo, and keys readable before Enter
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        try:
            yield
        finally:
            typed = b"".join(iter(lambda: os.read(fd, 1024), b"")).decode(errors="replace")
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            lines, self._partial = split_typed(self._partial, typed)
            self._lines += lines

    def read(self, prompt: Text) -> str:
        if self._lines:
            line = self._lines.pop(0)
            report.console.print(prompt, line, sep="")
            return line
        prefill, self._partial = self._partial, ""
        if prefill and readline is not None:
            readline.set_startup_hook(lambda: readline.insert_text(prefill))
        try:
            return report.console.input(prompt)
        finally:
            if readline is not None:
                readline.set_startup_hook(None)


@dataclass
class Conversation:
    system: str | None = None
    turns: list[ChatCompletionMessageParam] = field(default_factory=list)

    def messages(self, user_text: str) -> list[ChatCompletionMessageParam]:
        """What the model sees for a new user message: the system prompt, the turns so far, then it."""
        system: list[ChatCompletionMessageParam] = [{"role": "system", "content": self.system}] if self.system else []
        return [*system, *self.turns, {"role": "user", "content": user_text}]

    def record(self, user_text: str, reply: str) -> None:
        self.turns += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]


@dataclass(frozen=True)
class Reply:
    text: str
    seconds: float
    ttft: float | None  # time to the first streamed token
    tokens: int | None  # completion tokens, as the gateway counted them
    interrupted: bool


def stream_reply(
    client: openai.OpenAI,
    model: str,
    messages: list[ChatCompletionMessageParam],
    session: str,
    on_text: Callable[[str], None],
) -> Reply:
    """Stream one answer, handing each piece of text to `on_text`; Ctrl-C stops it early."""
    start = time.perf_counter()
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        user=session,
        extra_body={"metadata": {"session_id": session, "tags": ["chat"]}},
    )
    parts: list[str] = []
    ttft = tokens = None
    interrupted = False
    try:
        for chunk in stream:
            if chunk.usage:
                tokens = chunk.usage.completion_tokens
            text = chunk.choices[0].delta.content if chunk.choices else None
            if text:
                ttft = ttft if ttft is not None else time.perf_counter() - start
                parts.append(text)
                on_text(text)
    except KeyboardInterrupt:
        stream.close()
        interrupted = True
    return Reply("".join(parts), time.perf_counter() - start, ttft, tokens, interrupted)


def stats(reply: Reply) -> str:
    details = [f"{reply.seconds:.1f}s"]
    if reply.ttft is not None:
        details.append(f"first token {reply.ttft:.2f}s")
    if reply.tokens is not None:
        details.append(f"{reply.tokens} tokens")
    if reply.interrupted:
        details.append("stopped")
    return " · ".join(details)


def run(lab: Lab, model: str, system: str | None) -> None:
    lab.gateway.require()
    client = lab.gateway.client(lab.env.get(SUPPORT_BOT.env_var), timeout_s=120)
    conversation = Conversation(system)
    keyboard = Keyboard()
    session = run_id("chat")
    report.pairs(
        ("Model", f"{model}, through the gateway as {SUPPORT_BOT.alias}"),
        ("Session", f"{LANGFUSE_UI_URL}/project/{LANGFUSE_PROJECT_ID}/sessions/{session} (`llm-bonsai ui` first)"),
        ("Commands", "/reset, /exit, /help"),
        label_style="bold cyan",
    )
    while True:
        try:
            entered = parse(keyboard.read(Text("\nyou ", style="bold green")))
        except (EOFError, KeyboardInterrupt):
            entered = Command.EXIT
        if entered is Command.EXIT:
            report.note("\nbye")
            return
        if entered is Command.HELP:
            report.pairs(*HELP)
            continue
        if entered is Command.RESET:
            conversation.turns.clear()
            report.note("conversation cleared: the model starts from scratch")
            continue
        if entered is Command.UNKNOWN:
            report.note("unknown command; /help lists them (nothing was sent)")
            continue
        if not entered:
            continue

        report.console.print(Text("model ", style="bold magenta"), end="")
        try:
            with keyboard.quiet():
                reply = stream_reply(
                    client, model, conversation.messages(entered), session,
                    lambda text: report.console.print(text, end="", soft_wrap=True),
                )  # fmt: skip
        except openai.APIStatusError as error:
            report.line(report.http(error.status_code), f" {error.message}")
            continue
        except openai.APIConnectionError as error:
            report.line(("the gateway didn't answer: ", "red"), str(error))
            continue
        report.console.print()
        report.note(stats(reply))
        if reply.text:
            conversation.record(entered, reply.text)

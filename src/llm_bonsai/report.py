"""Terminal output, so every command reads the same way.

A probe prints `header()` first (the key it runs as and the prompt it sends), then what
happened (tables, status lines), then `summary()`: only what this run showed, plus at most
one clearly labelled "not shown here" point.

Nothing printed is parsed as rich markup or emoji codes: model answers, error messages
and log lines are full of brackets and colons.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from rich import box
from rich.console import Console, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Column, Table
from rich.text import Text

console = Console(markup=False, emoji=False, highlight=False)
err_console = Console(stderr=True, markup=False, emoji=False, highlight=False)


def line(*parts: str | Text | tuple[str, str], style: str = "") -> None:
    """One line built from plain strings, Texts and (text, style) pairs.

    Soft-wrapped: the terminal wraps it, so URLs and commands survive copy and paste.
    """
    console.print(Text.assemble(*parts, style=style), soft_wrap=True)


def note(text: str) -> None:
    console.print(text, style="dim", soft_wrap=True)


def show(renderable: RenderableType) -> None:
    """A table or panel, set off from what came before by a blank line."""
    console.print()
    console.print(renderable)


def section(title: str) -> None:
    console.print()
    console.print(title, style="bold")


def step(number: int, total: int, name: str) -> None:
    console.print()
    console.rule(Text(f"[{number}/{total}] {name}", style="bold"), align="left", style="dim")


def pairs(*rows: tuple[str, str], label_style: str = "bold") -> None:
    """Labels on the left, values on the right, aligned."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=label_style)
    grid.add_column()
    for label, value in rows:
        grid.add_row(label, value)
    console.print(grid)


def header(key: str, prompt: str) -> None:
    pairs(("Key", key), ("Prompt", prompt), label_style="bold cyan")
    console.print()


def table(*columns: str | Column, title: str | None = None, caption: str | None = None) -> Table:
    return Table(
        *columns,
        title=title,
        caption=caption,
        caption_justify="left",
        caption_style="dim",
        title_justify="left",
        title_style="bold",
        box=box.SIMPLE_HEAD,
        header_style="bold",
        pad_edge=False,
    )


@contextmanager
def live(renderable: RenderableType) -> Iterator[None]:
    """Redraw `renderable` while the block runs. When stdout isn't a terminal, print it once
    at the end instead: rich's Live leaves the last line without a newline there."""
    if not console.is_terminal:
        yield
        console.print(renderable)
        return
    with Live(renderable, console=console, refresh_per_second=4):
        yield


def right(header: str) -> Column:
    return Column(header, justify="right")


def clipped(header: str) -> Column:
    """A column for free text that would otherwise wrap every row: one line each, cut with '…'."""
    return Column(header, no_wrap=True, overflow="ellipsis")


def http(status: int) -> Text:
    style = "green" if status < 300 else "yellow" if status < 500 else "red"
    return Text(f"HTTP {status}", style=style)


def passed(ok: bool) -> Text:
    return Text("PASS", style="bold green") if ok else Text("FAIL", style="bold red")


def bullets(points: Iterable[str]) -> Table:
    """A bulleted list whose wrapped lines stay indented under their text."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column()
    for point in points:
        grid.add_row("•", point)
    return grid


def summary(*shown: str, not_shown: str | None = None) -> None:
    points = [*shown, *([f"Not shown here: {not_shown}"] if not_shown else [])]
    show(panel("What you just saw", bullets(points)))


def panel(title: str, body: RenderableType, style: str = "blue") -> Panel:
    return Panel(body, title=Text(title, style="bold"), title_align="left", border_style=style, padding=(0, 1))


def error(message: str) -> None:
    err_console.print(Text.assemble(("error: ", "bold red"), message))

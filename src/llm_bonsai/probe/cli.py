from typing import Annotated

import typer

from llm_bonsai.probe import cache, chaos, fallback, limits, load, replicas, smoke

app = typer.Typer(
    help="Poke the running lab and explain what happened.",
    no_args_is_help=True,
)


@app.command("smoke")
def smoke_command() -> None:
    """Check chat, streaming, embeddings, model access and Langfuse tracing end to end."""
    if not smoke.run():
        raise typer.Exit(code=1)


app.command("fallback")(fallback.run)
app.command("limits")(limits.run)
app.command("cache")(cache.run)
app.command("chaos")(chaos.run)


@app.command("replicas")
def replicas_command(
    keep_replicas: Annotated[bool, typer.Option(help="Leave 2 gateway replicas running afterwards.")] = False,
) -> None:
    """Hit one rate limit through 2 gateway replicas, without and then with Valkey.

    Takes ~4 min: redeploys the gateway 3 times, ending at 1 replica.
    """
    replicas.run(keep_replicas)


def concurrency_levels(value: str) -> list[int]:
    try:
        levels = sorted({int(level) for level in value.split(",")})
    except ValueError as error:
        raise typer.BadParameter("expected comma-separated integers, e.g. 1,2,4,8") from error
    if any(level < 1 for level in levels):
        raise typer.BadParameter("concurrency levels must be at least 1")
    return levels


@app.command("load")
def load_command(
    concurrency: Annotated[
        str, typer.Option(help="Comma-separated concurrency levels, run one after another.")
    ] = "1,2,4,8",
    duration: Annotated[float, typer.Option(min=1, help="Seconds per concurrency level.")] = 20,
    model: Annotated[str, typer.Option(help="Gateway model group to send traffic to.")] = "chat-small",
    max_tokens: Annotated[int, typer.Option(min=1, help="Cap on generated tokens per request.")] = 64,
) -> None:
    """Sweep concurrency levels and report latency, TTFT (time to first token) and errors, to watch requests queue."""
    load.run(concurrency_levels(concurrency), duration, model, max_tokens)

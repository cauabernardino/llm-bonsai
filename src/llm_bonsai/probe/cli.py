from typing import Annotated

import typer

from llm_bonsai.assistant import Variant
from llm_bonsai.catalog import CHAT_SMALL
from llm_bonsai.lab import Lab
from llm_bonsai.probe import app as app_probe
from llm_bonsai.probe import cache, chaos, evaluation, fallback, limits, load, replicas, smoke

app = typer.Typer(
    help="Poke the running lab and explain what happened.",
    no_args_is_help=True,
)


@app.command("smoke")
def smoke_command(ctx: typer.Context) -> None:
    """Check chat, streaming, embeddings, model access and Langfuse tracing end to end."""
    if not smoke.run(Lab.of(ctx)):
        raise typer.Exit(code=1)


@app.command("fallback")
def fallback_command(ctx: typer.Context) -> None:
    """Call chat-broken and show the gateway's fallback to chat-small."""
    fallback.run(Lab.of(ctx))


@app.command("limits")
def limits_command(ctx: typer.Context) -> None:
    """Hit a 3 requests/minute limit, then spend through a $0.05 budget (~1.5 min: waits out the window)."""
    limits.run(Lab.of(ctx))


@app.command("cache")
def cache_command(ctx: typer.Context) -> None:
    """Show a cache hit, an exact-match miss and a request that skips the cache."""
    cache.run(Lab.of(ctx))


@app.command("chaos")
def chaos_command(ctx: typer.Context) -> None:
    """Delete the gateway pod under load and measure the outage (~2 min)."""
    chaos.run(Lab.of(ctx))


@app.command("replicas")
def replicas_command(
    ctx: typer.Context,
    keep_replicas: Annotated[bool, typer.Option(help="Leave 2 gateway replicas running afterwards.")] = False,
) -> None:
    """Hit one rate limit through 2 gateway replicas, without and then with Valkey.

    Takes ~2.5 min: redeploys the gateway 3 times, ending at 1 replica.
    """
    replicas.run(Lab.of(ctx), keep_replicas)


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
    ctx: typer.Context,
    concurrency: Annotated[
        str, typer.Option(help="Comma-separated concurrency levels, run one after another.")
    ] = "1,2,4,8",
    duration: Annotated[float, typer.Option(min=1, help="Seconds per concurrency level.")] = 20,
    model: Annotated[str, typer.Option(help="Gateway model group to send traffic to.")] = CHAT_SMALL,
    max_tokens: Annotated[int, typer.Option(min=1, help="Cap on generated tokens per request.")] = 64,
) -> None:
    """Sweep concurrency levels and report latency, TTFT (time to first token) and errors, to watch requests queue."""
    load.run(Lab.of(ctx), concurrency_levels(concurrency), duration, model, max_tokens)


PromptOption = Annotated[Variant, typer.Option("--prompt", help="Which version of the support-answer prompt to use.")]


@app.command("app")
def app_command(
    ctx: typer.Context,
    prompt: PromptOption = Variant.baseline,
    question: Annotated[str, typer.Option(help="What the customer asks.")] = "How long does a refund take?",
) -> None:
    """Ask the support assistant one question and show its nested trace in Langfuse."""
    app_probe.run(Lab.of(ctx), prompt, question)


@app.command("eval")
def eval_command(
    ctx: typer.Context,
    prompt: PromptOption = Variant.baseline,
    judge: Annotated[bool, typer.Option(help="Also score answers with chat-small as an LLM judge.")] = True,
) -> None:
    """Score the support assistant over a Langfuse dataset, as one experiment run (~40 s)."""
    evaluation.run(Lab.of(ctx), prompt, judge)

"""Ask the support assistant one question and show the trace it leaves in Langfuse.

The app opens the trace (support-assistant → retrieval → answer); the gateway adds its
own generation under `answer`, because the app sends the W3C `traceparent` of that span
along with the request.
"""

from dataclasses import dataclass

from langfuse import propagate_attributes

from llm_bonsai import report
from llm_bonsai.assistant import PROMPT_NAME, SupportAssistant, Variant, ensure_prompt
from llm_bonsai.catalog import SUPPORT_BOT
from llm_bonsai.clients.langfuse import run_id, trace_url
from llm_bonsai.lab import Lab
from llm_bonsai.models import Observation

ROOT, RETRIEVAL, ANSWER = "support-assistant", "retrieval", "answer"


@dataclass(frozen=True)
class TraceShape:
    nested: bool  # retrieval and answer both sit under the support-assistant span
    gateway: Observation | None  # the gateway's generation, if it landed under `answer`
    prompt: str | None  # the prompt version linked to `answer`, e.g. "support-answer v2"


def ancestors(observation: Observation, by_id: dict[str, Observation]) -> list[str]:
    names = []
    while observation.parent_id in by_id:
        observation = by_id[observation.parent_id]
        names.append(observation.name)
    return names


def shape(observations: list[Observation]) -> TraceShape:
    by_id = {o.id: o for o in observations}
    named = {o.name: o for o in observations}
    nested = all(name in named and ancestors(named[name], by_id)[-1:] == [ROOT] for name in (RETRIEVAL, ANSWER))
    gateway = next(
        (o for o in observations if o.type == "GENERATION" and o.name != ANSWER and ANSWER in ancestors(o, by_id)),
        None,
    )
    answer = named.get(ANSWER)
    prompt = f"{answer.prompt_name} v{answer.prompt_version}" if answer and answer.prompt_name else None
    return TraceShape(nested, gateway, prompt)


def tree(observations: list[Observation]) -> list[str]:
    """One indented line per observation, children in start order."""
    children: dict[str | None, list[Observation]] = {}
    known = {o.id for o in observations}
    # Siblings in start order; on a tie (same millisecond), the shorter one most likely ran first.
    for o in sorted(observations, key=lambda o: (o.start_time, o.latency)):
        children.setdefault(o.parent_id if o.parent_id in known else None, []).append(o)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for o in children.get(parent, []):
            details = [o.type.lower()]
            if o.prompt_name:
                details.append(f"prompt {o.prompt_name} v{o.prompt_version}")
            if o.total_tokens:
                details.append(f"{o.model}, {o.total_tokens} tokens, ${o.total_cost:.4f}")
            lines.append(f"  {'   ' * depth}{o.name} ({'; '.join(details)}) {o.latency * 1000:.0f} ms")
            walk(o.id, depth + 1)

    walk(None, 0)
    return lines


def takeaways(trace: TraceShape) -> list[str]:
    bullets = []
    if trace.nested:
        bullets.append(
            f"One trace holds the whole request: the {ROOT} span, with {RETRIEVAL} and {ANSWER} under it, so "
            "you can see how long retrieval took next to the model call."
        )
    else:
        bullets.append(f"The spans didn't nest as {ROOT} → {RETRIEVAL} / {ANSWER}; check the tree above.")
    if trace.gateway:
        bullets.append(
            f"The gateway's own generation ({trace.gateway.total_tokens} tokens, ${trace.gateway.total_cost:.4f}) "
            f"sits under {ANSWER}: the app sent its span's W3C traceparent header and LiteLLM's OTel callback "
            "joined that trace instead of starting its own. App and gateway telemetry line up with no shared "
            "code, just a header."
        )
    else:
        bullets.append(
            "The gateway's generation didn't show up under the app's trace within 60 s; it may have started a "
            "separate trace (look for this run's user id in Langfuse)."
        )
    if trace.prompt:
        bullets.append(
            f"The {ANSWER} generation is linked to {trace.prompt}, so Langfuse can break latency, cost and "
            "scores down by prompt version (Prompts → support-answer → Metrics)."
        )
    return bullets


def run(lab: Lab, variant: Variant, question: str) -> None:
    lab.gateway.require()
    sdk = lab.langfuse.sdk
    prompt = ensure_prompt(sdk, variant)
    report.header(f"{SUPPORT_BOT.alias} (long-lived key from `llm-bonsai setup teams`)", repr(question))
    assistant = SupportAssistant(sdk, lab.gateway.client(lab.env.get(SUPPORT_BOT.env_var), timeout_s=60), prompt)
    user_id = run_id("app")
    with propagate_attributes(user_id=user_id, tags=["probe-app"]):
        answer = assistant.answer(question)
    sdk.flush()

    print(f"Retrieved: {', '.join(s.id for s in answer.snippets) or 'nothing'}")
    print(f"Prompt:    {PROMPT_NAME} v{prompt.version} (label {variant})")
    print(f"Answer:    {answer.text}")

    print(f"\nLangfuse trace {answer.trace_id} (waiting for the gateway's part to arrive):")
    observations = lab.langfuse.wait_for_trace(answer.trace_id, lambda found: shape(found).gateway is not None)
    print("\n".join(tree(observations)))
    print(f"  UI: `llm-bonsai ui`, then {trace_url(answer.trace_id)}")

    report.summary(
        *takeaways(shape(observations)),
        not_shown=f"how the other prompt version answers: `llm-bonsai probe app --prompt "
        f"{'strict' if variant is Variant.baseline else 'baseline'}`, or `llm-bonsai probe eval` to score "
        "both over a dataset.",
    )

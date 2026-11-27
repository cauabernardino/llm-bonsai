"""A tiny support assistant: keyword retrieval over a hard-coded FAQ, then one LLM call
through the gateway with a prompt managed in Langfuse.

`probe app` runs it once and shows its trace; `probe eval` runs it over a dataset. Each
answer is one trace: support-assistant → retrieval → answer, with the gateway's own
generation (tokens, cost) nested under `answer` through a W3C `traceparent` header.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import langfuse as lf
import openai
from langfuse.api import NotFoundError
from langfuse.model import ChatMessageDict, ChatMessageWithPlaceholdersDict, ChatPromptClient
from openai.types.chat import ChatCompletionMessageParam

from llm_bonsai.catalog import CHAT_SMALL


@dataclass(frozen=True)
class Snippet:
    id: str
    text: str


FAQ = [
    Snippet("refund-window", "Refunds: customers can request a full refund within 30 days of purchase."),
    Snippet(
        "refund-time", "Refund timing: approved refunds reach the original payment method in 5 to 10 business days."
    ),
    Snippet("shipping", "Shipping: standard delivery takes 3 to 5 business days; express delivery takes 1 day."),
    Snippet("payment-methods", "Payment methods: we accept Visa, Mastercard, PayPal and iDEAL."),
    Snippet("chargeback", "Chargebacks: a disputed card payment is answered with evidence within 7 days."),
    Snippet("currency", "Currencies: prices are shown in euros; cards are charged in EUR, USD or GBP."),
    Snippet("cancel", "Cancellations: an order can be cancelled free of charge until it ships."),
    Snippet("support-hours", "Support hours: the support team answers from 9:00 to 17:00 CET, Monday to Friday."),
]

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "be",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "we",
        "what",
        "when",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    ]
)


def tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in STOPWORDS}


def retrieve(question: str, k: int = 2) -> list[Snippet]:
    """The `k` snippets sharing the most words with the question (none if nothing overlaps)."""
    wanted = tokens(question)
    scored = [(len(wanted & tokens(snippet.text)), snippet) for snippet in FAQ]
    ranked = sorted((pair for pair in scored if pair[0] > 0), key=lambda pair: -pair[0])  # stable: FAQ order on ties
    return [snippet for _, snippet in ranked[:k]]


class Variant(StrEnum):
    """Two versions of the same prompt, told apart by their Langfuse label."""

    baseline = "baseline"
    strict = "strict"


PROMPT_NAME = "support-answer"
PROMPTS: dict[Variant, list[ChatMessageDict | ChatMessageWithPlaceholdersDict]] = {
    Variant.baseline: [
        {"role": "system", "content": "You are a helpful support assistant.\n\nContext:\n{{context}}"},
        {"role": "user", "content": "{{question}}"},
    ],
    Variant.strict: [
        {
            "role": "system",
            "content": "Answer the customer in one sentence, using only the context below. Quote exact "
            "numbers, days and names from it. If the context doesn't answer the question, say you don't "
            "know.\n\nContext:\n{{context}}",
        },
        {"role": "user", "content": "{{question}}"},
    ],
}
ANSWER_MAX_TOKENS = 80


def messages(prompt: Sequence[Mapping[str, Any]]) -> list[tuple[Any, Any]]:
    """(role, content) pairs: what decides whether two prompt versions say the same thing."""
    return [(m.get("role"), m.get("content")) for m in prompt]


def find_prompt(sdk: lf.Langfuse, variant: Variant) -> ChatPromptClient | None:
    """The version labelled `variant`, or None.

    No retries and no SDK warning: a 404 is expected on the first run, not a network blip.
    """
    logger = logging.getLogger("langfuse")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        return sdk.get_prompt(PROMPT_NAME, label=variant, type="chat", cache_ttl_seconds=0, max_retries=0)
    except NotFoundError:
        return None
    finally:
        logger.setLevel(level)


def ensure_prompt(sdk: lf.Langfuse, variant: Variant) -> ChatPromptClient:
    """The version labelled `variant`, creating a new one if it is missing or differs from PROMPTS."""
    wanted = PROMPTS[variant]
    current = find_prompt(sdk, variant)
    if current is not None and messages(current.prompt) == messages(wanted):
        return current
    labels = [variant.value, "production"] if variant is Variant.baseline else [variant.value]
    return sdk.create_prompt(
        name=PROMPT_NAME, prompt=wanted, labels=labels, type="chat", commit_message=f"{variant} variant (llm-bonsai)"
    )


@dataclass(frozen=True)
class Answer:
    text: str
    snippets: list[Snippet]
    trace_id: str


class SupportAssistant:
    def __init__(self, sdk: lf.Langfuse, client: openai.OpenAI, prompt: ChatPromptClient) -> None:
        self._sdk = sdk
        self._client = client
        self.prompt = prompt

    def answer(self, question: str) -> Answer:
        with self._sdk.start_as_current_observation(
            name="support-assistant", as_type="span", input={"question": question}
        ) as root:
            with self._sdk.start_as_current_observation(name="retrieval", as_type="retriever", input=question) as span:
                snippets = retrieve(question)
                span.update(output=[snippet.id for snippet in snippets])
            context = "\n".join(f"- {snippet.text}" for snippet in snippets) or "(nothing relevant found)"
            messages = cast(list[ChatCompletionMessageParam], self.prompt.compile(context=context, question=question))
            with self._sdk.start_as_current_observation(
                name="answer",
                as_type="generation",
                input=messages,
                model=CHAT_SMALL,
                model_parameters={"temperature": 0, "max_tokens": ANSWER_MAX_TOKENS},
                prompt=self.prompt,  # links the prompt version to this generation
            ) as generation:
                # No usage here: the gateway's generation, nested under this one, carries tokens and cost.
                traceparent = f"00-{generation.trace_id}-{generation.id}-01"
                response = self._client.chat.completions.create(
                    model=CHAT_SMALL,
                    messages=messages,
                    extra_headers={"traceparent": traceparent},
                    temperature=0,
                    max_tokens=ANSWER_MAX_TOKENS,
                )
                text = (response.choices[0].message.content or "").strip()
                generation.update(output=text)
            root.update(output=text)
        return Answer(text, snippets, root.trace_id)

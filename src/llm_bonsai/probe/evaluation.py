"""Score the support assistant over a small Langfuse dataset, as one experiment run per prompt version.

Every answer is scored by a deterministic keyword check and, optionally, by chat-small
acting as a judge. Run it once per prompt version and compare the runs in Langfuse.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import langfuse as lf
import openai
from langfuse import Evaluation
from langfuse.api import NotFoundError
from langfuse.experiment import ExperimentItemResult

from llm_bonsai import report
from llm_bonsai.assistant import PROMPT_NAME, SupportAssistant, Variant, ensure_prompt
from llm_bonsai.catalog import CHAT_SMALL, SUPPORT_BOT
from llm_bonsai.clients.langfuse import Langfuse, experiments_url
from llm_bonsai.lab import Lab

if TYPE_CHECKING:  # the SDK doesn't re-export it
    from langfuse._client.datasets import DatasetClient

DATASET = "support-faq"


@dataclass(frozen=True)
class Case:
    id: str  # fixed, so re-runs update the item instead of adding a copy
    question: str
    answer: str
    must_mention: tuple[str, ...]  # the answer passes the keyword check if it mentions any of these

    @property
    def input(self) -> dict[str, str]:
        return {"question": self.question}

    @property
    def expected_output(self) -> dict[str, Any]:
        return {"answer": self.answer, "must_mention": list(self.must_mention)}


CASES = [
    Case("support-faq-01", "How long do I have to ask for a refund?", "Within 30 days of purchase.", ("30 days",)),
    Case("support-faq-02", "When will my refund arrive?", "In 5 to 10 business days.", ("5 to 10", "5-10")),
    Case("support-faq-03", "How fast is express delivery?", "1 day.", ("1 day", "one day", "next day")),
    Case("support-faq-04", "Can I pay with PayPal?", "Yes, PayPal is accepted.", ("yes",)),
    Case("support-faq-05", "How soon do you answer a chargeback?", "With evidence within 7 days.", ("7 days",)),
    Case("support-faq-06", "Which currencies can my card be charged in?", "EUR, USD or GBP.", ("usd", "gbp")),
    Case("support-faq-07", "Can I cancel my order?", "Yes, free of charge until it ships.", ("until it ship",)),
    Case("support-faq-08", "When is support available?", "9:00 to 17:00 CET, Monday to Friday.", ("9:00", "17:00")),
]  # fmt: skip


def mentions(answer: str, options: list[str]) -> bool:
    """Whether `answer` contains any option as whole words, ignoring case ("30 days" doesn't match "130 days")."""
    text = answer.lower()
    return any(re.search(rf"(?<!\w){re.escape(option.lower())}", text) for option in options)


def keyword_check(*, input: Any, output: Any, expected_output: Any, metadata: Any, **kwargs: Any) -> Evaluation:
    options: list[str] = expected_output["must_mention"]
    hit = mentions(str(output), options)
    return Evaluation(name="keyword", value=1.0 if hit else 0.0, comment=f"looked for: {' / '.join(options)}")


# Tried on known good/bad answers: with "1 (wrong) to 5" first, chat-small replies 1 to everything; with
# this rubric it replies 5 to everything. Either way the score follows the prompt, not the answer.
JUDGE_PROMPT = """QUESTION: {question}
REFERENCE ANSWER: {reference}
CANDIDATE ANSWER: {answer}

Does the candidate answer give the same facts as the reference answer? Score it: 5 = same facts, 3 = partly,
1 = different or missing. Reply with the score only."""
JUDGE_MAX_TOKENS = 10


def parse_rating(reply: str) -> int | None:
    """The rating if the reply holds exactly one number, from 1 to 5; anything else is a parse failure."""
    numbers = re.findall(r"\d+(?:\.\d+)?", reply)
    return int(numbers[0]) if len(numbers) == 1 and numbers[0] in {"1", "2", "3", "4", "5"} else None


def judge_with(client: openai.OpenAI) -> Callable[..., list[Evaluation]]:
    """An evaluator that asks chat-small to rate an answer from 1 to 5.

    It records `judge-parsed` (1/0, with the raw reply as comment) for every item, and
    `judge` only when the reply parsed, so failures are counted rather than scored.
    """

    def judge(*, input: Any, output: Any, expected_output: Any, metadata: Any, **kwargs: Any) -> list[Evaluation]:
        prompt = JUDGE_PROMPT.format(question=input["question"], reference=expected_output["answer"], answer=output)
        response = client.chat.completions.create(
            model=CHAT_SMALL, messages=[{"role": "user", "content": prompt}], temperature=0, max_tokens=JUDGE_MAX_TOKENS
        )
        reply = (response.choices[0].message.content or "").strip()
        rating = parse_rating(reply)
        evaluations = [Evaluation(name="judge-parsed", value=0.0 if rating is None else 1.0, comment=reply)]
        if rating is not None:
            evaluations.append(Evaluation(name="judge", value=float(rating), comment=reply))
        return evaluations

    return judge


def ensure_dataset(sdk: lf.Langfuse) -> "DatasetClient":
    """The dataset with every case in CASES; items are only written when missing or changed, so
    re-runs don't create a new dataset version and runs stay comparable."""
    try:
        existing = {item.id: item for item in sdk.get_dataset(DATASET).items}
    except NotFoundError:
        sdk.create_dataset(name=DATASET, description="Support FAQ questions with the facts an answer must mention.")
        existing = {}
    for case in CASES:
        item = existing.get(case.id)
        if item is None or item.input != case.input or item.expected_output != case.expected_output:
            sdk.create_dataset_item(
                dataset_name=DATASET, id=case.id, input=case.input, expected_output=case.expected_output
            )
    return sdk.get_dataset(DATASET)


@dataclass(frozen=True)
class Row:
    id: str
    question: str
    answer: str
    keyword: bool
    rating: int | None
    judge_reply: str | None  # None when the judge was off


def row(result: ExperimentItemResult) -> Row:
    scores = {evaluation.name: evaluation for evaluation in result.evaluations}
    parsed = scores.get("judge-parsed")
    judged = scores.get("judge")
    item = result.item
    assert not isinstance(item, dict), "the experiment runs on a Langfuse dataset, so items are DatasetItems"
    return Row(
        id=item.id,
        question=item.input["question"],
        answer=str(result.output),
        keyword=bool(scores["keyword"].value) if "keyword" in scores else False,
        rating=int(judged.value) if judged else None,
        judge_reply=parsed.comment if parsed else None,
    )


def latest_experiment(langfuse: Langfuse, variant: Variant) -> str | None:
    """The id of the most recent experiment run with this prompt variant, read from its item spans' metadata."""
    spans = langfuse.observations(name="experiment-item-run", limit="100")
    matching = (s.metadata.get("experiment_id") for s in spans if s.metadata.get("variant") == variant)
    return next((str(found) for found in matching if found), None)


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def shorten(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def takeaways(rows: list[Row], total: int, judge: bool) -> list[str]:
    bullets = []
    if len(rows) < total:
        bullets.append(f"{total - len(rows)} of {total} items failed (error or timeout) and have no scores.")
    hits = sum(r.keyword for r in rows)
    bullets.append(
        f"Keyword check: {hits} of {len(rows)} answers mention the expected fact. It is deterministic, so the "
        "same answer always gets the same score, but it only knows the words it was given."
    )
    if not judge:
        return bullets
    rated = [(r, r.rating) for r in rows if r.rating is not None]
    unparsed = len(rows) - len(rated)
    if not rated:
        bullets.append(f"Judge: none of the {unparsed} replies was a single number 1–5, so there is no judge score.")
        return bullets
    mean = sum(rating for _, rating in rated) / len(rated)
    failures = ""
    if unparsed:
        failures = f", and {plural(unparsed, 'parse failure')} (replies that weren't a single number 1–5)"
    bullets.append(f"Judge (chat-small rating 1–5): mean {mean:.1f} over {plural(len(rated), 'answer')}{failures}.")

    calibration = (
        "A 0.5B judge is unreliable: before trusting any LLM judge, score a sample by hand and check the judge "
        "against those human labels (calibration)."
    )
    ratings = {rating for _, rating in rated}
    contradictions = sum(1 for r, rating in rated if (r.keyword and rating <= 2) or (not r.keyword and rating >= 4))
    if len(rated) > 1 and len(ratings) == 1 and contradictions:
        verdict = (
            f"The judge gave all {len(rated)} answers it rated a {rated[0][1]}, including {contradictions} the "
            "keyword check contradicts: it isn't judging the answer at all."
        )
    elif len(rated) > 1 and len(ratings) == 1:
        verdict = (
            f"The judge gave all {len(rated)} answers it rated a {rated[0][1]}, and the keyword check agrees with "
            "each, so this run can't tell a correct judge from one that always says the same number."
        )
    elif contradictions:
        verdict = (
            f"The judge contradicted the keyword check on {plural(contradictions, 'answer')} (a high rating for an "
            "answer missing the fact, or the reverse)."
        )
    else:
        verdict = f"Judge and keyword check agreed on every rated answer, but {len(rows)} items prove little."
    bullets.append(f"{verdict} {calibration}")
    return bullets


def run(lab: Lab, variant: Variant, judge: bool) -> None:
    lab.gateway.require()
    sdk = lab.langfuse.sdk
    prompt = ensure_prompt(sdk, variant)
    dataset = ensure_dataset(sdk)
    client = lab.gateway.client(lab.env.get(SUPPORT_BOT.env_var), timeout_s=60)
    assistant = SupportAssistant(sdk, client, prompt)
    # Unique per run: runs with the same name on one dataset merge into one experiment. Our own id, kept in
    # the run's metadata, is how the next run of the other variant finds this one to compare against.
    run_name = f"{variant} v{prompt.version} ({datetime.now():%m-%d %H:%M:%S})"
    experiment_id = lf.Langfuse.create_trace_id()
    previous = latest_experiment(lab.langfuse, other_variant(variant))
    report.header(
        f"{SUPPORT_BOT.alias} (long-lived key from `llm-bonsai setup teams`), for the app and the judge",
        f"the {len(dataset.items)} questions of dataset {DATASET}, with prompt {PROMPT_NAME} v{prompt.version} "
        f"(label {variant})",
    )
    print(f"Experiment run {run_name!r}: one item at a time (~5 s each{' with the judge' if judge else ''})")

    def task(*, item: Any, **kwargs: Any) -> str:
        question: str = item.input["question"]
        print(f"  · {question}", flush=True)
        return assistant.answer(question).text

    # One at a time: the task is synchronous, so the SDK's event loop runs items serially anyway,
    # and Ollama on CPU gains little from parallel requests (see `probe load`).
    result = dataset.run_experiment(
        name=f"{DATASET} eval",
        run_name=run_name,
        description=f"Prompt {PROMPT_NAME} v{prompt.version} ({variant}); keyword check"
        + (" and chat-small judge" if judge else ""),
        task=task,
        evaluators=[keyword_check, judge_with(client)] if judge else [keyword_check],
        max_concurrency=1,
        experiment_id=experiment_id,
        metadata={"prompt": f"{PROMPT_NAME} v{prompt.version}", "variant": variant, "experiment_id": experiment_id},
    )

    rows = sorted((row(item_result) for item_result in result.item_results), key=lambda r: r.id)
    print("\n #  keyword  judge  question → answer")
    for n, r in enumerate(rows, start=1):
        rating = str(r.rating) if r.rating is not None else ("?" if judge else "–")
        keyword = "pass" if r.keyword else "miss"
        print(f" {n}  {keyword:<7}  {rating:<5}  {shorten(r.question, 40)} → {shorten(r.answer, 70)}")
    unparsed = Counter(r.judge_reply for r in rows if judge and r.rating is None)
    if unparsed:
        replies = ", ".join(f"{reply!r} ×{n}" if n > 1 else repr(reply) for reply, n in unparsed.most_common())
        print(f"Judge replies that didn't parse (shown as ?): {replies}")
    print(f"\nUI (`llm-bonsai ui` first): {experiments_url(experiment_id)}")
    if previous:
        print(f"Side by side with the latest {other_variant(variant)} run: {experiments_url(previous, experiment_id)}")

    report.summary(
        *takeaways(rows, len(dataset.items), judge),
        not_shown=None
        if previous
        else f"the comparison. Run `llm-bonsai probe eval --prompt {other_variant(variant)}` to get a run of the "
        "other prompt version, and a link that shows both side by side.",
    )


def other_variant(variant: Variant) -> Variant:
    return Variant.strict if variant is Variant.baseline else Variant.baseline

"""Output conventions, so every probe reads the same way:

- `header()` first: which key the probe runs as and the prompt it sends;
- statuses as `HTTP 200` / `HTTP 429` / `HTTP 422`;
- `summary()` last: what *this run* showed, plus clearly labelled "not shown here" points.
"""


def header(key: str, prompt: str) -> None:
    print(f"Key:    {key}")
    print(f"Prompt: {prompt}\n")


def summary(*shown: str, not_shown: str | None = None) -> None:
    print("\nWhat you just saw")
    print("─" * 17)
    for point in shown:
        print(f"• {point}")
    if not_shown:
        print(f"• Not shown here: {not_shown}")

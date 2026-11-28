# Shortcuts for the common flows. The CLI is the real interface: `uv run llm-bonsai --help`.
CLI := uv run llm-bonsai

.PHONY: preflight cluster up down nuke bootstrap smoke ui check

preflight:
	$(CLI) setup preflight

cluster:
	$(CLI) setup cluster

up:
	$(CLI) setup all

down:
	$(CLI) teardown

nuke:
	$(CLI) teardown --cluster

bootstrap:
	$(CLI) setup teams

smoke:
	$(CLI) probe smoke

# Langfuse on localhost:3000 and the LiteLLM UI on localhost:4000, until Ctrl-C.
ui:
	$(CLI) ui

# Lint, type-check (mypy and pyright, both strict) and unit-test the package (no cluster needed).
check:
	uv run ruff check
	uv run ruff format --check
	uv run mypy
	uv run pyright
	uv run pytest

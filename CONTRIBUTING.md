# Contributing

Issues and pull requests are welcome. For anything larger than a fix, open an issue first so we can agree on the shape.

## Dev loop

```bash
git clone https://github.com/cauabernardino/llm-bonsai && cd llm-bonsai
uv sync                      # Python 3.12+, dev tools included
make check                   # ruff, mypy and pyright (strict), pytest: what CI runs, plus `uv build`
uv run llm-bonsai --help     # the CLI from your checkout; its .env lives here
```

`make check` needs no cluster: the tests cover the pure logic (parsers, takeaways, preflight rules, argv building) against fakes. Anything that talks to the lab is checked by running it: `uv run llm-bonsai probe smoke` is the end-to-end test. Say in the PR which probes you ran, and on what machine.

## Ground rules

A PR that breaks one of these needs to say why.

- **No paid API keys.** Everything runs on local models. A cloud model may appear only as a commented-out example.
- **No Bitnami images or charts.** Use official images, with plain manifests when there's no official chart.
- **One replica of everything, small models, tight limits.** Probes that need more (like `probe replicas`) scale up and put things back.
- **Stay inside the memory budget.** The cluster gets 14 GiB. A new piece needs requests and limits, and its measured RAM goes in the [memory budget](README.md#memory-budget) table.
- **Pin every version.** No `latest`, no floating chart versions.
- **Teach.** Output says what happened, with real numbers, and only claims what that run showed.

## Where things live

- **Chart versions and the Kubernetes version:** `src/llm_bonsai/setup/infra.py`. Images installed by a chart follow its version; the first line of each values file records which.
- **Image tags of the plain manifests** (Postgres, Valkey, Prometheus): the manifests under `src/llm_bonsai/infra/`.
- **Tool floors and host requirements:** `src/llm_bonsai/setup/preflight.py`. The kubectl floor follows the Kubernetes version.
- **Python dependencies:** `pyproject.toml` sets the ranges, `uv.lock` pins them. Run `uv lock` after changing them; CI fails on a stale lock.

When you bump a version, update the [Pinned versions](README.md#pinned-versions) table, the version comment at the top of the values file, and, for Kubernetes or a tool floor, the requirements table in the README.

Model groups, teams and their limits are in `src/llm_bonsai/catalog.py`. The gateway's model list is in `src/llm_bonsai/infra/litellm/values.yaml`.

## Adding a probe

1. Add a module under `src/llm_bonsai/probe/` with a `run(lab: Lab)` function.
2. Get clients from `lab`, never build your own. That way port-forwards close when the command ends.
3. Follow the conventions in `report.py`. `probe smoke`, a pass/fail check, is the one exception.
   - `report.header` names the key and the prompt.
   - Tables show what happened.
   - `report.summary` ends the probe with "What you just saw", plus at most one `not_shown=` point.
4. Build the bullets in a pure function (`takeaways(...)` in most probes) and test it in `tests/test_takeaways.py`. Include the branch where the lab misbehaves.
5. Register the command in `src/llm_bonsai/probe/cli.py` and in the command tree in `tests/test_cli.py`.
6. Add a row to the probe table in the README.

## Code and commits

- Type everything. `make check` runs both mypy and pyright in strict mode.
- Comments explain why, not what. Leave out notes about how the code came to be.
- Commits get a title that says what changed and a short body that says why. One concern per commit. Pull requests are squash-merged, so the PR title becomes the commit title.

## Releasing

For maintainers. Releases are cut by hand, whenever there's something worth shipping. The commit titles since the last tag become the release notes, and the [Releases](https://github.com/cauabernardino/llm-bonsai/releases) page is the changelog.

1. Open a pull request that bumps the version: `uv version --bump minor` (or `patch`, `major`) updates `pyproject.toml` and `uv.lock`. Title it `Release X.Y.Z`.
2. Once it's merged, tag the merge commit and push only the tag:

   ```bash
   git switch main && git pull
   git tag "v$(uv version --short)"
   git push origin "v$(uv version --short)"   # the release workflow checks, builds and creates the GitHub Release
   ```

The workflow refuses a tag that isn't on `main` or doesn't match the version in `pyproject.toml`. If it fails, fix the cause through a pull request, then move the tag to the new commit: `git push origin :refs/tags/vX.Y.Z`, tag again and push.

Before 1.0, a minor bump may break things. When one does, add a line saying so to its release notes (`gh release edit` or the Releases page).

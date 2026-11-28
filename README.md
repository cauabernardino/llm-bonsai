# llm-bonsai

> A real GenAI platform, deliberately kept tiny. Same structure, carefully pruned to fit a laptop.

llm-bonsai is a hands-on lab that runs the core building blocks of a production GenAI platform on a single Mac, inside a local Kubernetes cluster, for free:

- an **LLM gateway** ([LiteLLM](https://docs.litellm.ai/)): one OpenAI-compatible endpoint with virtual keys per team, budgets, rate limits, fallbacks and caching;
- a **self-hosted model** ([Ollama](https://ollama.com/) running `qwen2.5:0.5b` on CPU), standing in for a GPU serving stack;
- **LLM observability and evals** ([Langfuse](https://langfuse.com/)): traces, prompt versions, datasets and scored experiment runs;
- the **state** a gateway needs to scale out: Postgres for keys/teams/spend, Valkey (an open-source Redis fork) for shared rate limits and caching.

Everything runs locally, so you need **no paid API keys**. Infrastructure is Helm charts and plain YAML. Everything else goes through one small, typed Python CLI, `llm-bonsai`, run with [`uv`](https://docs.astral.sh/uv/): `setup` builds the lab step by step, `probe` pokes at it and explains what happened.

**This is a home-lab for study purposes, not a production setup.** Each piece is the real thing (official images and charts, real config), only sized down: one replica, small models, tight memory limits. Building it phase by phase shows you how the pieces depend on each other and where they break.

## Architecture

```mermaid
flowchart LR
  PY["llm-bonsai CLI / demo app"] --> LL["LiteLLM proxy (gateway)"]
  LL --> OL["Ollama: qwen2.5:0.5b (CPU)"]
  LL --> PG[("Postgres: keys, teams, spend")]
  LL --> VK[("Valkey: cache + shared limits")]
  LL -- "OpenTelemetry callback" --> LF["Langfuse"]
  PY -- "Langfuse SDK spans" --> LF
  LF --> LFS[("ClickHouse, Postgres, Valkey, S3-compatible storage")]
```

Two namespaces: `llm` (Ollama, LiteLLM, Postgres, Valkey) and `observability` (Langfuse).

| Component | Plays the role of |
|---|---|
| LiteLLM proxy | The LLM gateway: single endpoint, per-team keys, budgets/limits, fallbacks, caching |
| Ollama + a 0.5B model | A cheap stand-in for self-hosted model serving (vLLM/TGI on GPUs in production) |
| Langfuse | Tracing, prompt management, datasets and evals |
| Postgres | The gateway's system of record: keys, teams, budgets, spend logs |
| Valkey | Shared state across gateway replicas: rate-limit counters and the response cache |

## Status

| Phase | What | Status |
|---|---|---|
| 0 | Preflight and local cluster | ✅ |
| 1 | Local model with Ollama | ✅ |
| 2 | LiteLLM gateway (Postgres, Valkey, teams, keys) | ✅ |
| 3 | Langfuse: tracing the gateway | ✅ |
| 4 | Gateway probes: fallback, limits, cache, replicas, chaos, load test | ✅ |
| 4b | Tooling packaged as the `llm-bonsai` CLI | ✅ |
| 5 | App tracing with nested spans, prompt versions, a mini eval | ✅ |
| 6 | Prometheus metrics (optional): the gateway's `/metrics`, scraped by one Prometheus pod | ✅ |
| 7 | vLLM on CPU (optional) | planned |
| 8 | Argo CD (optional) | planned |

## Requirements

- **Apple Silicon Mac with 24 GB of RAM or more.** The cluster takes 14 GB. The stack was sized for that budget; with less RAM you'll have to trim it.
- **~40 GB of free disk.**
- **Docker Desktop, recent version**, with **Memory set to ≥ 17 GB** (Settings → Resources). The extra ~3 GB above the cluster's 14 GB is for the Docker VM itself; see [Troubleshooting](#troubleshooting).
- [Homebrew](https://brew.sh/), plus `kubectl`, `helm`, `minikube` and `uv`:

  ```bash
  brew install kubernetes-cli helm minikube uv
  ```

  Docker Desktop ships its own `kubectl`, which can be too old for a current cluster (kubectl supports ±1 minor version of the server). Homebrew's `kubectl` takes precedence on Apple Silicon.

> [OrbStack](https://orbstack.dev/) with its built-in Kubernetes is a lighter alternative to Docker Desktop + minikube. This guide uses minikube; `make preflight` (`llm-bonsai setup preflight`) recommends OrbStack if it finds it installed.

### Memory budget

Rough sizes measured on this stack:

| Piece | RAM |
|---|---|
| LiteLLM (single pod) | ~1.2 GiB steady, ~1.8 GiB peak at startup |
| Ollama with `qwen2.5:0.5b` loaded | ~1 GiB |
| Postgres + Valkey | ~120 MiB |
| Kubernetes system pods | ~0.7 GiB |
| Langfuse stack (web, worker, ClickHouse, Keeper, Postgres, Valkey, SeaweedFS) | ~1.8 GiB idle; ClickHouse grows with data, capped at 1.5 GiB |
| cert-manager + ClickHouse operator | ~100 MiB |

Every container has CPU/memory requests and limits. If memory gets tight, drop optional pieces in this order: Argo CD → vLLM → Prometheus → the second LiteLLM replica.

## Quick start

```bash
make preflight      # check the machine; read the recommendation at the end
make cluster        # minikube: 6 CPUs, 14 GB, metrics-server
make up             # everything: Ollama, Postgres, Valkey, Langfuse (+ prerequisites), LiteLLM
                    # ~6 min the first time, mostly image pulls
make bootstrap      # create teams and their virtual keys, saved to .env
make smoke          # end-to-end check through the gateway
make ui             # Langfuse on localhost:3000, LiteLLM's UI on localhost:4000 (+ Prometheus on :9090 if installed), until Ctrl-C
```

**No port-forward to babysit.** Every command that talks to the gateway or Langfuse opens its own `kubectl port-forward` on a free local port, reconnects it if the pod behind it goes away, and closes it when the command exits. Only `make ui` holds fixed ports (3000, 4000, and 9090 for the optional Prometheus), for your browser and for `curl`, and only while it runs.

The `make` targets are shortcuts for the CLI, which does all the work. `make up` is `uv run llm-bonsai setup all`, which runs these steps in order; each one can also be run on its own, and all are safe to re-run:

```bash
uv run llm-bonsai --help                 # setup / probe / ui / teardown
uv run llm-bonsai setup --help           # preflight, cluster, namespaces, secrets, ollama, postgres, valkey,
                                         # cert-manager, clickhouse-operator, langfuse, litellm, all, teams
uv run llm-bonsai setup langfuse         # e.g. re-apply only Langfuse after editing its values
uv run llm-bonsai probe --help           # smoke, fallback, limits, cache, replicas, chaos, load, app, eval
```

Tip: `source .venv/bin/activate` once per shell and drop the `uv run` prefix.

Tear down:

```bash
make down           # llm-bonsai teardown: uninstall the stack, including volumes
                    # (keeps cert-manager and the ClickHouse operator)
make nuke           # llm-bonsai teardown --cluster: delete the whole minikube cluster
```

Both ask before deleting anything (`--yes` skips the question). Every `kubectl` and `helm` call is pinned to the `minikube` context, so the CLI never touches another cluster; set `LLM_BONSAI_CONTEXT` to use a different one.

Secrets live in `.env` (gitignored). `llm-bonsai setup secrets` (run by `make up`) generates random dev-only values the first time and never rotates them afterwards. See `.env.example` for the variables.

## Repository layout

```
Makefile                  shortcuts for the CLI, plus `make check`
infra/
  namespaces.yaml
  ollama/values.yaml      Helm values for the Ollama chart
  postgres/postgres.yaml  StatefulSet + Service (official postgres image)
  valkey/valkey.yaml      Deployment + Service (official valkey image)
  litellm/values.yaml     Helm values for the LiteLLM chart, including the model config
  cert-manager/values.yaml
  langfuse/values.yaml    Helm values for the Langfuse chart, sized for a laptop
  prometheus/prometheus.yaml  optional: one Prometheus pod scraping the gateway (Phase 6)
src/llm_bonsai/           the Python package behind the `llm-bonsai` command
  cli.py                  the root command: setup / probe / ui / teardown
  lab.py                  Lab: the clients one command uses, closed (with its port-forwards) when it ends
  config.py               LabConfig (repo root, kube context) and EnvFile (.env, the only source of credentials)
  catalog.py              what the lab consists of: namespaces, Services, model groups, teams
  models.py               shared records: Chart, Service, Team, GatewayPod, Observation, Attempt
  assistant.py            the demo support assistant: FAQ retrieval, Langfuse-managed prompt, traced answer
  report.py               the output conventions every probe follows
  clients/                how the package reaches the lab
    shell.py              subprocesses, with failures as LabErrors
    kube.py               Kube: kubectl/helm pinned to the lab's context
    tunnels.py            port-forwards that live as long as the command that needs them
    gateway.py            Gateway (traffic, health, throwaway keys) and GatewayAdmin (teams, keys)
    langfuse.py           Langfuse: the Python SDK (traces, prompts, datasets) and reading observations back
  setup/                  `llm-bonsai setup …`
    preflight.py          machine checks and cluster recommendation
    credentials.py        random dev secrets into .env, then into Kubernetes Secrets
    infra.py              pinned charts, the cluster, and Installer (one method per install step)
    teams.py              teams + virtual keys
    prometheus.py         Prometheus's scrape key: a read-only admin limited to /metrics
  probe/                  `llm-bonsai probe …`
    smoke.py              end-to-end gateway check, including traces in Langfuse
    fallback.py limits.py cache.py replicas.py chaos.py
    load.py               asyncio load generator: latency, TTFT, error rate per concurrency
    app.py evaluation.py  the support assistant's nested trace; its eval over a Langfuse dataset
tests/                    unit tests for the pure logic (`make check`, no cluster needed)
```

`make check` runs `ruff` (lint and format), `mypy --strict` and `pytest`.

No Bitnami images or charts are used anywhere: Bitnami changed its free image catalog in 2025, and old references break. Postgres and Valkey use the official images with plain manifests.

---

## Phase 0: Preflight and local cluster

**What it does.** `make preflight` is read-only. It reports RAM, CPU model, free disk, the Docker VM's size, which tools are installed and which kube contexts exist, and then recommends how to create the cluster. The CPU model matters later: vLLM on CPU wants `bfloat16`, which M2 and newer chips support and M1 doesn't.

`make cluster` creates a minikube cluster on the Docker driver with 6 CPUs and 14 GB, and enables metrics-server so `kubectl top` works.

**What to expect at the end.**

```bash
$ kubectl get nodes
NAME       STATUS   ROLES           AGE   VERSION
minikube   Ready    control-plane   1m    v1.37.0

$ kubectl top node
NAME       CPU(cores)   CPU(%)   MEMORY(bytes)   MEMORY(%)
minikube   145m         1%       668Mi           4%
```

**Worth knowing.** The node advertises the **Docker VM's** CPUs and memory as its capacity, not minikube's real limits (`docker inspect minikube` shows the actual 6 CPU / 14 GiB cgroup). The scheduler therefore thinks it has more room than exists. Pods can be overcommitted without any warning, and if their combined usage passes 14 GiB, processes get OOM-killed inside the node container instead of Kubernetes evicting pods cleanly. That's why honest requests/limits and watching `kubectl top` matter here.

## Phase 1: Local model with Ollama

**What it does.** Installs the [otwld/ollama](https://github.com/otwld/ollama-helm) chart into `llm`, pulls `qwen2.5:0.5b` (chat) and `nomic-embed-text` (embeddings) onto a 5 Gi volume, and caps the pod at 3 CPUs / 2 GiB.

**What to expect at the end.** The `ollama` pod is Ready. The chart pulls models in a `postStart` hook, and Kubernetes doesn't run readiness probes until `postStart` has finished, so Ready already means the models are on disk.

**Try it.** In normal use, clients go through the gateway (Phase 2). To talk to Ollama directly, open a port-forward in its own terminal:

```bash
kubectl -n llm port-forward svc/ollama 11434:11434
```

then:

```bash
curl localhost:11434/api/tags                       # lists both models
curl localhost:11434/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Why is the sky blue?"}]}'
curl localhost:11434/api/embed -d '{"model":"nomic-embed-text","input":"hello"}'
curl localhost:11434/api/ps                         # what's loaded, and how big
```

Other ways in:

- your local `ollama` CLI: `OLLAMA_HOST=localhost:11434 ollama run qwen2.5:0.5b` (stop any local Ollama app first, since it also wants port 11434);
- no port-forward at all: `kubectl -n llm exec -it deploy/ollama -- ollama run qwen2.5:0.5b`.

**Worth knowing.**

- **It runs on CPU only.** The model runs inside the Linux VM, where Apple's Metal GPU isn't visible. The logs show `inference compute id=cpu library=cpu`. Ollama sizes itself against the container's memory limit, not the node's.
- **`OLLAMA_NUM_PARALLEL=2`** is set explicitly (the default is "backend decides"). Up to 2 requests per model run at once; the rest **queue**. The Phase 4 load test makes this visible: latency jumps once concurrency exceeds 2. That's why serving is scaled on queue depth, not CPU.
- **`OLLAMA_KEEP_ALIVE=24h`** (default 5 minutes) keeps the model loaded between experiments, so cold loads don't distort latency measurements.
- **The context window defaults to 4096 tokens on CPU.** The KV cache grows with `context × parallel requests`; with the model loaded, Ollama reports ~690 MiB.

## Phase 2: LiteLLM gateway

**What it does.**

1. **Postgres** (`postgres:16-alpine`, 1 Gi volume) and **Valkey** (`valkey/valkey`, in-memory only).
2. **LiteLLM** from the official chart in **monolith mode**: one pod serving the OpenAI-compatible API, the management API and the admin UI. The chart's default splits these into three Deployments at 4 GiB each, which doesn't fit a laptop.
3. Three model names behind the gateway:

   | Model name | Backed by | Purpose |
   |---|---|---|
   | `chat-small` | `qwen2.5:0.5b` on Ollama | the working chat model, with **fake prices** so budgets can actually run out |
   | `chat-broken` | an address that doesn't exist (5 s timeout, no retries) | falls back to `chat-small`, for the fallback probe |
   | `embed` | `nomic-embed-text` on Ollama | embeddings |

4. **`make bootstrap`** creates two teams, each with one virtual key:

   | Team | Allowed models | Limits |
   |---|---|---|
   | `payments-ai` | `chat-small`, `chat-broken` | 3 requests/min, $1000 budget |
   | `support-bot` | `chat-small`, `embed` | 600 requests/min, $1000 budget |

   The keys are written to `.env` as `LITELLM_KEY_PAYMENTS_AI` and `LITELLM_KEY_SUPPORT_BOT`. It's safe to re-run: existing teams are reused, and valid keys are kept with their limits updated to match `TEAMS` in `src/llm_bonsai/setup/teams.py`, so changing a limit there and re-running `make bootstrap` applies it.

   The budgets are generous on purpose. Load and chaos runs spend a few fake dollars each, and the Phase 4 probes that need to *run out* of budget create throwaway keys with a tiny one.

**What to expect at the end.**

```
$ make smoke
PASS  chat completion (0.3s): 'Red.' via chat-small, 42 tokens
PASS  streaming (0.3s): 3 chunks → 'Red.'
PASS  embeddings (0.6s): 768-dim vector
PASS  forbidden model (chat-broken) (0.0s): rejected with HTTP 403
all smoke checks passed
```

The smoke test uses the `support-bot` key and the stock OpenAI Python SDK. Pointing the SDK's `base_url` at the gateway is all a client needs to change.

**Try it.** With `make ui` running in another terminal:

```bash
set -a; source .env; set +a        # load the keys into your shell

# chat as the support-bot team
curl localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY_SUPPORT_BOT" -H 'Content-Type: application/json' \
  -d '{"model":"chat-small","messages":[{"role":"user","content":"Tell me a joke"}]}'

# which models can this key use?
curl localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_KEY_SUPPORT_BOT"

# spend per key and per team (admin calls use the master key)
curl "localhost:4000/key/info?key=$LITELLM_KEY_SUPPORT_BOT" -H "Authorization: Bearer $LITELLM_MASTER_KEY"
curl localhost:4000/team/list -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

From Python, it's the plain OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:4000/v1", api_key="<a team key from .env>")
print(client.chat.completions.create(
    model="chat-small", messages=[{"role": "user", "content": "Hi!"}]
).choices[0].message.content)
```

**Admin UI:** http://localhost:4000/ui. Log in with user `admin` and the value of `LITELLM_MASTER_KEY` as the password. It shows teams, keys, spend and request logs.

**Worth knowing.**

- **Spend lags by a few seconds.** LiteLLM writes spend to Postgres in batches, not on every request.
- **Local models cost $0**, so `chat-small` carries made-up `input_cost_per_token`/`output_cost_per_token` values. Without them, no budget could ever be reached. One short chat costs ~$0.005; `llm-bonsai probe limits` (Phase 4) shows what happens when a key reaches its budget.
- **`drop_params: true`.** The OpenAI SDK sends `encoding_format=base64` on embeddings by default, and the Ollama provider rejects it with a 400. Dropping params a provider doesn't support keeps the gateway transparent to clients. The trade-off is that unsupported parameters are now ignored silently.
- **The chart doesn't ship a database**, and it never builds `DATABASE_URL` itself. It passes `DATABASE_HOST/PORT/USER/NAME/PASSWORD` and the image assembles (and URL-encodes) the URL. Database migrations run as a Helm **pre-install hook Job**, so Postgres must be up before LiteLLM is installed; `make up` does it in that order.
- Settings in the chart worth reading for production: an in-pod PgBouncer (`database.connectionPool`), a `collector` sidecar that moves spend accounting off the request path, a `/health/drain` preStop hook for graceful shutdowns, and HPA on requests/tokens per second through Prometheus Adapter.

## Phase 3: Langfuse, tracing the gateway

**What it does.**

1. Installs two cluster-wide prerequisites: **cert-manager**, and the official **ClickHouse operator**. Langfuse chart 2.x doesn't run ClickHouse itself; it creates `ClickHouseCluster` and `KeeperCluster` resources, and the operator turns them into pods. The operator gets its webhook certificates from cert-manager.
2. Installs **Langfuse v4** into `observability` with all its stores, each sized down from production defaults:

   | Store | What Langfuse uses it for |
   |---|---|
   | ClickHouse (+ Keeper) | traces, observations and scores: the analytical data. Keeper coordinates ClickHouse; it's required even for one replica. |
   | Postgres | users, projects, API keys, prompts, datasets: the transactional data |
   | Valkey | job queues between `langfuse-web` (API/UI) and `langfuse-worker` (ingestion) |
   | SeaweedFS | S3-compatible blob storage for raw event payloads and media |

3. **Headless init:** `LANGFUSE_INIT_*` environment variables create the organisation, the project `llm-bonsai`, its API keys and an admin user on first boot. The keys come from `.env`, so LiteLLM and the CLI can use them without anyone clicking through the UI.
4. **Wires LiteLLM to Langfuse** with the `langfuse_otel` callback. Every gateway request (successes and failures) is exported as an OpenTelemetry span to Langfuse's OTLP endpoint.

**What to expect at the end.**

```
$ make smoke
PASS  chat completion (0.4s): 'Red' via chat-small, 41 tokens
PASS  streaming (1.9s): 25 chunks → 'Rainbow is usually associated with ...'
PASS  embeddings (0.7s): 768-dim vector
PASS  forbidden model (chat-broken) (0.0s): rejected with HTTP 403
PASS  traced in Langfuse (4.2s): 2 generations for smoke-6762fc98; chat-small: 64 tokens, 1.88s, $0.0089, team tag present
PASS  failed call traced in Langfuse (4.1s): ERROR generation: litellm.APIConnectionError: ...
all smoke checks passed
```

The two new checks poll Langfuse's API until this run's traces appear. That usually takes a few seconds, because ingestion is asynchronous: web → queue → worker → ClickHouse.

**Try it.** Run `make ui`, open http://localhost:3000 and log in with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` from `.env`. Under **Tracing**, each gateway request is a `litellm_request` generation with model, tokens, latency, cost and tags. Filter by the tag `user_api_key_team_alias:support-bot` to see one team's traffic, or by level `ERROR` for failures.

Send a request with your own user id and tags:

```bash
set -a; source .env; set +a
curl localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY_SUPPORT_BOT" -H 'Content-Type: application/json' \
  -d '{"model":"chat-small","user":"alice","metadata":{"tags":["experiment-1"]},
       "messages":[{"role":"user","content":"Hello"}]}'
```

From the OpenAI SDK, the same thing is `user="alice", extra_body={"metadata": {"tags": ["experiment-1"]}}`.

Read traces back through the API (project keys as basic auth):

```bash
curl -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
  "localhost:3000/api/public/v2/observations?type=GENERATION&userId=alice&fields=core,basic,usage,model,metadata"
```

**Worth knowing.**

- **Use `langfuse_otel`, not LiteLLM's classic `langfuse` callback.** Langfuse v4 disables the legacy ingestion API by default, so the classic callback would silently log nothing.
- **Team tags come from the gateway, not the client.** `langfuse_default_tags: ["user_api_key_alias", "user_api_key_team_alias"]` makes LiteLLM tag every trace with the key and team that authenticated the request. Clients can add tags (`metadata.tags`) but can't remove or fake these. That's what makes per-team cost and quality views trustworthy.
- **The OpenAI `user` field becomes the Langfuse user id.**
- **Failures are traced, authorization rejections aren't.** An upstream error is recorded as an `ERROR` generation with the exception message. A 403 for a forbidden model is rejected before any LLM call, so it never reaches the tracing callback; that's what the gateway's own logs and spend tables are for.
- **Langfuse v4 reads are observation-based.** The old `GET /api/public/traces` endpoint answers that it's unavailable in v4 "events_only" mode; use `GET /api/public/v2/observations`. Ask for field groups with `fields=`. `type`, `userId` and `level` work as filters, but **`tags` is silently ignored**, which is why the smoke test finds its traces by a unique user id.
- **Sizing:** the chart's defaults are production-sized (100 Gi for ClickHouse, three Keeper replicas, 8 GiB requests). Here ClickHouse runs with `max_server_memory_usage` at 1.5 GiB under a 2 GiB limit, so under pressure it fails a query instead of getting OOM-killed. The whole stack idles at ~1.8 GiB.
- **Langfuse's Valkey must not evict** (`maxmemory-policy noeviction`, enforced by the chart), because it holds job queues. The gateway's Valkey is the opposite (`allkeys-lru`) because it only holds a cache and counters. Same software, different job.
- **All credentials live in one `langfuse` Secret built from `.env`**, following the chart's own minimal-installation example. Letting the chart generate passwords also works, but a reinstall could regenerate them while old volumes still expect the previous ones.
- **On first install, `langfuse-web` and ClickHouse restart a few times.** Web tries to run ClickHouse migrations before the operator has created ClickHouse's Service, and ClickHouse waits for Keeper. It sorts itself out in a minute or two.

## Phase 4: Gateway probes

**What it does.** Connects Valkey to LiteLLM, then runs one probe per gateway behaviour. Every probe prints the same three things:

1. **which key** it runs as, and the **prompt** it sends;
2. what happened, with real numbers;
3. a short **"What you just saw"**: only what this run demonstrated, plus anything worth knowing that it *didn't* show, labelled **"Not shown here"**.

Each opens its own port-forwards, so nothing else needs to be running. If the gateway doesn't answer, the probe says so and stops instead of crashing.

Two config changes come with this phase (`infra/litellm/values.yaml`):

- `redis.host` points at Valkey, so rate limits and spend counters are **shared across gateway replicas**;
- a **response cache** in Valkey, **opt-in** per request (`cache_params.mode: default_off`). With caching on for everything, the limits, budget and load probes would measure the cache instead of the model, and cached answers cost $0, so no budget could ever be reached.

The fallback, limits and replicas probes create a **throwaway key** with `payments-ai`'s limits and delete it at the end. Re-running them never uses up the long-lived keys from `make bootstrap` (`llm-bonsai setup teams`). The others use the `support-bot` key.

| Command | Shows | Takes |
|---|---|---|
| `uv run llm-bonsai probe fallback` | A dead model answered by its fallback | seconds |
| `uv run llm-bonsai probe limits` | Rate limit (3/min), then a $0.05 budget running out | ~1.5 min |
| `uv run llm-bonsai probe cache` | One prompt, four requests: model, cache hit, parameter change, no opt-in | seconds |
| `uv run llm-bonsai probe replicas` | 2 gateway replicas with and without shared state | ~2.5 min |
| `uv run llm-bonsai probe chaos` | Deleting the gateway pod under load | ~1.5 min |
| `uv run llm-bonsai probe load` | Latency and TTFT at concurrency 1, 2, 4 and 8 | ~1.5 min |

The outputs below are real runs (summaries left out). Your timings will differ.

### Fallback

```
Key:    throwaway key, team payments-ai (deleted at the end)
Prompt: 'In one sentence: what is a payment refund?'

Requested model chat-broken → HTTP 200 in 1.00s
Response headers:
  x-litellm-model-group: chat-small
  x-litellm-attempted-fallbacks: 1
  x-litellm-model-api-base: http://ollama.llm.svc.cluster.local:11434
Answer: A payment refund is the process of returning the amount originally owed to a recipient after receiving an online payment.

Langfuse, user fallback-75f784a6:
    start     took  outcome
  +   0ms      5ms  ERROR  does-not-exist.llm.svc.cluster.local:1234 [Name or service not known]
  +  18ms    922ms  ok     ollama_chat/qwen2.5:0.5b
  UI: `llm-bonsai ui`, then http://localhost:3000 → Tracing → filter User ID = fallback-75f784a6
```

- **The client asked for `chat-broken` and got a normal HTTP 200.** Only the `x-litellm-*` response headers reveal the failover.
- **Langfuse keeps both attempts under one user id.** It records the backend rather than the model name the client asked for, so the dead host in the `ERROR` line is how you tell which deployment failed.
- **This backend failed in 5 ms**, on a DNS error. A backend that *hangs* instead costs up to `timeout` × (1 + `num_retries`) before the fallback starts, which is why `chat-broken` sets `timeout: 5` and `num_retries: 0`.
- **Faking a failure:** LiteLLM can do it with `"mock_testing_fallbacks": true` in a request, but current versions reject it unless an admin sets `general_settings.dangerously_allow_mock_testing_request_params: true`. Otherwise any caller could force failovers.

### Limits and budgets

```
Key:    throwaway key, team payments-ai: 3 requests/min, $0.05 budget (deleted at the end)
Prompt: part 1 'hi' (max_tokens=1) · part 2 a 1996-char refund policy (max_tokens=20, ~$0.04 each):
        Summarise this refund policy in one sentence. Customers may return items within thirty day…

Part 1: rate limit, 10 requests in a row
  #1  HTTP 200
  #2  HTTP 200
  #3  HTTP 200
  #4  HTTP 429  retry-after: 60  (window resets in 60s)
  #5  HTTP 429
  ⋮   (#6–#10 the same; abridged)
  → 3 of 10 allowed

… waiting 60s for the rate-limit window to reset

Part 2: budget, expensive requests until the gateway says no (budget $0.05)
  spend so far: $0.0096 (part 1)
  #1  HTTP 200  $0.0096 → $0.0522  ← crossed $0.05, still allowed
  #2  HTTP 422  Budget has been exceeded

Spend, summed from x-litellm-response-cost headers: $0.0522
Spend in Postgres (/key/info) right now:            $0.0096
```

- **Rate limits return HTTP 429** with `retry-after`: back off, then try again.
- **Budgets are checked before a request, and cost is added after it.** The request that crosses the budget succeeds; only later ones are refused. You can therefore overspend by up to one request's cost, so cap `max_tokens` or leave headroom. This probe sends requests one at a time. Recent LiteLLM versions also hold a reservation for each in-flight request's estimated cost, but concurrent requests aren't tested here.
- **Budget refusals are HTTP 422** in this version ("Budget has been exceeded"), not 429. Retrying won't help until someone raises the budget.
- **Postgres lags behind.** The gateway enforces limits from its own counters in Valkey, while `/key/info` and the UI read Postgres, which is written in batches.

### Cache

```
Key:    support-bot (long-lived key from `llm-bonsai setup teams`)
Prompt: 'Write a haiku about a bonsai tree. (run df295a)'
        the random run id makes request #1 a guaranteed cache miss

 #  request                         took  cache  cost
 1  opt in                       1199 ms  miss   $0.0097
 2  opt in, same as #1             20 ms  HIT    $0.0000
 3  opt in, temperature=0.1       487 ms  miss   $0.0085
 4  no opt-in                    1194 ms  off    $0.0099

Answer #1 (model): Bonsai's roots deep,   / Leaves of light, shadows weaved—   / Spring growth, each drop.
Answer #2 (cache): Bonsai's roots deep,   / Leaves of light, shadows weaved—   / Spring growth, each drop.  ← identical to #1
Answer #4 (model): In a few years, it'll be a sapling, / Bonsai tree standing tall, / New growth fills the space.
```

To opt in, send `"cache": {"use-cache": true}` (from the OpenAI SDK: `extra_body={"cache": {"use-cache": True}}`). What each request shows:

- **#2** is a hit: about 60× faster, $0, and word for word #1's answer.
- **#3** changed only `temperature` and missed: the cache key covers model, messages *and* parameters. This is exact-match caching.
- **#4** didn't opt in, so the model ran and wrote a different haiku.

### Replicas: why shared state matters

```
Key:    throwaway key per scenario, team payments-ai: 3 requests/min (deleted at the end)
Prompt: 'hi' × 16 (max_tokens=1), sent from a pod inside the cluster

Scenario 1: 2 replicas, no Valkey
  redeploying the gateway: 2 replicas, Valkey off (helm upgrade, ~1 min)…
  gateway: 2 ready replica(s), REDIS_HOST=unset
  16 requests through the Service → 6 × HTTP 200, 10 × HTTP 429
    litellm-6c74b6d5c8-d2jmp: 3 × HTTP 200, 2 × HTTP 429
    litellm-6c74b6d5c8-n7bk7: 3 × HTTP 200, 8 × HTTP 429

Scenario 2: 2 replicas, Valkey for shared state
  redeploying the gateway: 2 replicas, Valkey on (helm upgrade, ~1 min)…
  gateway: 2 ready replica(s), REDIS_HOST=valkey.llm.svc.cluster.local
  16 requests through the Service → 3 × HTTP 200, 13 × HTTP 429
    litellm-6448bb6878-q6c7d: 1 × HTTP 200, 4 × HTTP 429
    litellm-6448bb6878-z6q44: 2 × HTTP 200, 9 × HTTP 429

Restoring the normal gateway
  redeploying the gateway: 1 replica, Valkey on (helm upgrade, ~1 min)…
  gateway: 1 ready replica(s), REDIS_HOST=valkey.llm.svc.cluster.local
```

The per-pod lines are counted from each pod's access log, and they make the point visible:

- **Without Valkey, each pod enforced its own 3/minute** (3 + 3), so the effective limit doubled.
- **With Valkey, both pods shared one counter** (1 + 2 = 3).

How the probe works:

- It redeploys the gateway for each scenario (the equivalent of `llm-bonsai setup litellm --set gateway.replicaCount=2 ...`), and **always** restores the single-replica config at the end, even if a step fails. Add `--keep-replicas` to leave two running.
- The burst comes from a short-lived **pod inside the cluster**, because `kubectl port-forward svc/litellm` sends everything to a single pod and would hide the load balancing.

### Chaos: deleting the gateway pod

```
Key:    support-bot (long-lived key from `llm-bonsai setup teams`)
Prompt: 'Explain in three sentences why the sky is blue.' (2 concurrent streams for 90s, max_tokens=32)

A gateway pod gets deleted at t=15s.

t= 15.1s  deleted pod litellm-6448bb6878-f2zmb
t= 36.0s  replacement pod Ready

Requests by start time (5s buckets):
    0–5  s  6 ok
    5–10 s  4 ok
   10–15 s  4 ok
   15–20 s  none started (each worker was still waiting on an earlier request)
   20–25 s  none started (each worker was still waiting on an earlier request)
   25–30 s  none started (each worker was still waiting on an earlier request)
   30–35 s  0 ok, 18 failed
   35–40 s  4 ok, 2 failed
   40–45 s  4 ok
   ⋮   (4 ok per bucket until the end; abridged)

Failed attempts: 20 (ConnectError, ReadError, RemoteProtocolError). Workers retry every 0.25s, so this counts retries, not users.
Outage: 20.3s, nearly all of it waiting for the replacement pod; the port-forward picked it up right away (timings ±1s)
```

**One replica means a full outage**, about 20 s here. It's almost all Kubernetes creating the replacement pod, LiteLLM starting up and its readiness probe passing.

- **In-flight requests** finished normally in this run: on SIGTERM the server stops taking new connections but completes what it has. Other runs can cut off a stream mid-way; the probe tells you which happened.
- **New requests** hung or failed.

Not shown here: with 2+ replicas, new connections go to the surviving pod. In production, pair that with a PodDisruptionBudget and a draining `preStop` hook (LiteLLM's `/health/drain`), so upgrades finish their streams first.

### Load test: queueing, not computing

```
Key:    support-bot (long-lived key from `llm-bonsai setup teams`)
Prompt: 'Explain in three sentences why the sky is blue.' (every request, streamed, max_tokens=64)

chat-small, 20s per concurrency level
lat = time to the full answer, ttft = time to first token; p50 = median, p95 = 95th percentile

conc  reqs  req/s  p50 lat  p95 lat p50 ttft p95 ttft  errors
   1     7   0.33    3.00s    3.74s    0.10s    0.12s  0
   2    10   0.47    4.31s    4.65s    0.20s    0.25s  0
   4    12   0.47    7.49s    9.67s    4.25s    5.22s  0
   8    16   0.38   17.48s   24.29s   12.95s   17.93s  0

(p95 over fewer than 20 requests is close to the slowest one; use --duration for more.)
```

How to read it:

- **1 → 2 concurrent requests:** TTFT (time to the first token, which is what a chat user feels) stays low, because Ollama serves `OLLAMA_NUM_PARALLEL=2` requests at once. The two share the same CPU, though, so each gets slower and throughput rises only about 40%, not 2×.
- **2 → 8:** throughput stays flat while latency and TTFT climb, because the extra requests are **waiting in a queue**, not computing.
- **Why it matters for scaling:** a CPU-based autoscaler sees the same busy CPU at 2 and at 8, so model serving scales on **queue depth** instead (for example vLLM's `num_requests_waiting`).

Options: `uv run llm-bonsai probe load --concurrency 1,2,4 --duration 30 --max-tokens 32 --model chat-small`. TTFT is measured as the time to the first streamed chunk with non-empty `delta.content`. It's a closed-loop test (each worker waits for its answer before sending the next), which is gentler than real traffic.

---

## Phase 5: App tracing and a mini eval

**What it does.** Adds a tiny "support assistant" (`src/llm_bonsai/assistant.py`), instrumented with the Langfuse Python SDK, and evaluates it over a dataset. It answers in two steps:

1. **retrieval:** keyword overlap over 8 hard-coded FAQ snippets (no embeddings);
2. **answer:** one call through the gateway with the support-bot key, using a prompt stored in **Langfuse prompt management**. The prompt `support-answer` has two versions, labelled `baseline` (also `production`) and `strict` ("one sentence, only from the context, quote exact numbers"). The probes create them, or a new version when the text in the code changed.

| Command | Shows | Takes |
|---|---|---|
| `uv run llm-bonsai probe app [--prompt strict] [--question "…"]` | One answer and its nested trace, read back from Langfuse | seconds |
| `uv run llm-bonsai probe eval [--prompt strict] [--no-judge]` | The assistant scored over an 8-item dataset, as one experiment run | ~40 s |

### App: one trace from the app down to the model

```
Retrieved: refund-window, refund-time
Prompt:    support-answer v1 (label baseline)
Answer:    A refund takes 5 to 10 business days to process.

Langfuse trace 7bd64953a2105d9930c1cd5511c1771a (waiting for the gateway's part to arrive):
  support-assistant (span) 1116 ms
     retrieval (retriever) 0 ms
     answer (generation; prompt support-answer v1) 1116 ms
        litellm_request (generation; chat-small, 84 tokens, $0.0099) 1035 ms
           raw_gen_ai_request (span) 1035 ms
```

How to read it:

- The app creates `support-assistant`, `retrieval` and `answer` with the SDK. The two levels below come from **the gateway**: the app sends the `answer` span's W3C `traceparent` header with the request, and LiteLLM's OpenTelemetry callback joins that trace instead of starting its own. One header is all it takes for app and gateway telemetry to line up.
- Tokens and cost sit on the gateway's generation only. The app's `answer` generation carries the prompt link and the input/output but no usage, so the trace's cost isn't counted twice.
- `answer` is **linked to the prompt version**, so Langfuse can break latency, cost and scores down by version (Prompts → support-answer).

### Eval: two prompt versions, scored

```
Experiment run 'strict v2 (11-28 00:56:20)': one item at a time (~5 s each with the judge)
 #  keyword  judge  question → answer
 1  pass     5      How long do I have to ask for a refund? → Customers can request a full refund within 30 days of purchase.
 2  miss     ?      When will my refund arrive? → Customers can request a full refund within 30 days of purchase.
 3  pass     5      How fast is express delivery? → Express delivery takes 1 day.
 4  pass     ?      Can I pay with PayPal? → Yes, you can pay with PayPal.
 5  miss     ?      How soon do you answer a chargeback? → I don't know.
 6  pass     5      Which currencies can my card be charged… → Your card can be charged in EUR, USD, or GBP.
 7  miss     ?      Can I cancel my order? → Yes, you can cancel your order.
 8  pass     5      When is support available? → The support team is available from 9:00 to 17:00 CET, Monday to Frida…
Judge replies that didn't parse (shown as ?): '5 = same facts, 3 = partly' ×2, '5 = same facts, 3 = partly,' ×2

UI (`llm-bonsai ui` first): http://localhost:3000/project/llm-bonsai/experiments/results?baseline=…
Side by side with the latest baseline run: http://localhost:3000/project/llm-bonsai/experiments/results?baseline=…&c=…
```

How to read it:

- **The dataset** (`support-faq`: 8 questions, each with a reference answer and the fact it must mention) is upserted with fixed item ids, and only when an item changed, so both prompt versions run against the same dataset version.
- **Each run is an experiment** (`dataset.run_experiment` in SDK v5, recorded through OpenTelemetry; Langfuse v4 has dropped the old dataset-run-items API). Run names carry a timestamp, because two runs with the same name would merge into one experiment.
- **Keyword check:** deterministic, so the same answer always gets the same score. It ignores case and matches whole words only ("30 days" doesn't match "130 days"), but it only knows the words it was given.
- **LLM judge:** `chat-small` rates each answer against the reference and must reply with a single number from 1 to 5. Anything else is a **parse failure**, recorded as a `judge-parsed = 0` score with the raw reply, not as a rating.
- **What the judge shows:** a 0.5B judge doesn't judge. With "1 (wrong) to 5" first in its prompt it replied `1` to every answer; with the rubric used now it replies `5`, or echoes the rubric back. The score follows the prompt's wording, not the answer, which is why a real judge gets **calibrated against human labels** before anyone trusts its numbers.
- `strict` answers are shorter and quote the context, and they show the model's limits too: #2 uses the wrong snippet, and #5 says "I don't know" although the chargeback snippet was retrieved.

Run the other version (`--prompt baseline` / `--prompt strict`) and the eval prints a link that opens both runs side by side.

---

## Phase 6: Gateway metrics

**What it does.** Turns on LiteLLM's `prometheus` callback (`litellm_settings.callbacks` in `infra/litellm/values.yaml`, next to `langfuse_otel`). The gateway then serves Prometheus text on `/metrics`, on its usual port 4000. It's part of the open-source proxy; nothing has to be licensed.

**Try it.** With `make ui` running in another terminal:

```sh
. ./.env   # loads LITELLM_MASTER_KEY into this shell (values in .env are quoted; sourcing strips the quotes)
curl -sL -H "Authorization: Bearer $LITELLM_MASTER_KEY" localhost:4000/metrics | grep '^litellm_'
```

After one `make smoke`, that's ~300 samples across ~75 metric names (abridged; `hashed_api_key`, ids and most labels cut):

```
litellm_proxy_total_requests_metric_total{api_key_alias="support-bot-key",requested_model="chat-small",route="/v1/chat/completions",status_code="200",team_alias="support-bot",…} 2.0
litellm_proxy_failed_requests_metric_total{requested_model="chat-broken",exception_class="Ollama_chat.APIConnectionError",exception_status="500",…} 1.0
litellm_spend_metric_total{api_key_alias="support-bot-key",model="qwen2.5:0.5b",requested_model="chat-small",team_alias="support-bot",…} 0.009
litellm_llm_api_time_to_first_token_metric_count{model_group="chat-small",…} 1.0
litellm_remaining_api_key_budget_metric{api_key_alias="support-bot-key",…} 999.991
litellm_remaining_team_budget_metric{team_alias="support-bot",…} +Inf
```

How to read it:

- **Auth:** `/metrics` needs the **master key**. No key, or a team's key, gets HTTP 401 ("Only proxy admin can …"). `require_auth_for_metrics_endpoint` defaults to `true` in v1.111; setting it to `false` opens the endpoint to anyone who can reach the port. A real Prometheus then needs a bearer token, which means an admin credential in its scrape config.
- **`-L` matters:** `/metrics` is a mounted sub-app, so the gateway answers `307` to `/metrics/` first. Without `-L` (or the trailing slash), curl prints the empty redirect body and the `grep` finds nothing.
- **The same questions as Langfuse, answered differently:** requests, failures, tokens, spend and latency (`litellm_request_total_latency_metric`, `…_llm_api_latency_metric`, time to first token), per key, team and model group. Langfuse keeps each request so you can drill into one; Prometheus keeps counters you can graph and alert on, which is how an on-call engineer finds out that fallbacks spiked at 3 am.
- **Budgets:** the keys have budgets (`remaining_api_key_budget` 999.991 of 1000), the teams don't, so their remaining budget is `+Inf`.
- **Even the rejected scrapes are counted:** they show up as `route="/metrics"`, `status_code="401"`.
- **Cardinality:** labels include `hashed_api_key`, `client_ip`, `user_agent` and `end_user`, so every new key or client adds series. That's harmless at lab scale, but at real scale it's the first thing to trim (`litellm_settings.prometheus_exclude_labels`).
- Not shown here: queue depth, which only the model server knows (vLLM's `num_requests_waiting`, Phase 7).

### Prometheus: the same metrics over time

```sh
uv run llm-bonsai setup prometheus   # optional, not part of `make up`; needs the gateway running
make ui                              # now also serves Prometheus on localhost:9090
```

This adds one Prometheus pod (`infra/prometheus/prometheus.yaml`: a plain manifest, a day of retention in an `emptyDir`, a 512Mi memory limit) that scrapes the gateway every 15 s. Prometheus has no login; its **Status → Targets** page should show each gateway pod as `UP`. Some queries to paste into its **Query** page:

| Question | PromQL |
|---|---|
| Requests per second, by team, model and status | `sum by (team_alias, requested_model, status_code) (rate(litellm_proxy_total_requests_metric_total{route="/v1/chat/completions"}[5m]))` |
| Spend per key in the last hour (fake prices) | `sum by (api_key_alias) (increase(litellm_spend_metric_total[1h]))` |
| Fallbacks, from which model group to which | `sum by (requested_model, fallback_model) (litellm_deployment_successful_fallbacks_total)` |
| p95 end-to-end latency per model group | `histogram_quantile(0.95, sum by (le, model_group) (rate(litellm_request_total_latency_metric_bucket[5m])))` |
| How many series the gateway exports | `count({job="litellm"})` |

The `rate`/`increase` queries only see traffic inside their window, so run `make smoke` a couple of times first. Over an idle window the latency quantile is `NaN` (0/0). After two smoke runs: p95 0.90 s for `chat-small`, 0.48 s for `embed`.

How it's wired:

- **Its own key, not the master key.** `setup prometheus` creates a read-only admin user (`proxy_admin_viewer`) and a key for it that may only call `/metrics` (`allowed_routes`), saves the key to `.env` as `LITELLM_KEY_PROMETHEUS`, and mounts it into the pod from a Secret (`authorization.credentials_file`, read again on every scrape). Measured: that key scrapes fine, but gets 403 on chat completions and on the management API. A plain team key can't scrape at all. A viewer key *without* the route limit could still call models and spend budget.
- **It scrapes pods, not the Service.** Prometheus lists the gateway pods through the Kubernetes API (`kubernetes_sd_configs`, allowed by a Role that can only read pods in `llm`). Scraping the Service would reach a different pod on each scrape once there are 2 replicas, so the counters would jump back and forth. With `--set gateway.replicaCount=2`, Targets showed both pods `UP`.
- **The path is `/metrics/`**, to skip the redirect.
- **Cardinality in practice:** after a smoke run and two `probe fallback` runs, the gateway exported ~620 series. Each throwaway probe key (`probe-fallback-…`) adds its own series, which stay around for the whole retention window even after the key is deleted.
- **It's cheap:** the pod used ~36 MiB.
- Not shown here: Grafana dashboards and alerting rules. In production you'd run this through the Prometheus Operator (kube-prometheus-stack), with a `ServiceMonitor` and a recording rule per question above.

---

## What to look at

With `make ui` running (log in to Langfuse with `LANGFUSE_INIT_USER_EMAIL` / `_PASSWORD` from `.env`, and to the LiteLLM UI as `admin` with `LITELLM_MASTER_KEY`):

- **Per-team spend:** LiteLLM UI → Teams, or `curl localhost:4000/team/list` with the master key. `payments-ai` and `support-bot` each have their own budget and spend.
- **A fallback trace:** run `llm-bonsai probe fallback`, then Langfuse → Tracing, filtered by the user id it prints: an ERROR generation for the dead backend, then the successful one.
- **A trace across app and gateway:** `llm-bonsai probe app` prints a link to its trace: the app's spans, the prompt version, and the gateway's generation underneath.
- **Prompt versions:** Langfuse → Prompts → `support-answer`: v1 `baseline`/`production`, v2 `strict`, and the generations that used each.
- **The dataset run comparison:** run `llm-bonsai probe eval` and `llm-bonsai probe eval --prompt strict`; the second prints a side-by-side link. Or Langfuse → Datasets → `support-faq`.
- **Replicas with and without Valkey:** `llm-bonsai probe replicas`. Without Valkey, 6 requests got through a 3/minute limit (each pod counted to 3); with Valkey, 3 did.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| LiteLLM pods exit immediately with **code 132** and no logs | `Illegal instruction`. Older Docker Desktop VMs on Apple Silicon advertise a CPU feature (SVE2) the chip doesn't have, and the newer C library in LiteLLM's image tries to use it. Check with `docker run --rm python:3.13-slim python -c "import ctypes; print(bool(ctypes.CDLL(None).getauxval(26) & 2))"`: `True` means affected. | Update Docker Desktop, then recreate the cluster (`make nuke cluster up`). |
| `docker inspect minikube` shows `OOMKilled=true` / the whole cluster stops | The Docker VM has barely more memory than minikube's limit, so the VM itself runs out (image unpacking makes it worse). | Give Docker Desktop ≥ 17 GB, then `minikube start`. Changing Docker's resources restarts its VM and stops minikube. |
| LiteLLM pod `OOMKilled`, `CrashLoopBackOff` | LiteLLM needs ~1.8 GiB while starting up, even though it settles at ~1.2 GiB. | Keep the memory limit ≥ 2.5 GiB (as in `infra/litellm/values.yaml`). |
| `helm upgrade`: `another operation (install/upgrade/rollback) is in progress` | A previous `helm` run was interrupted and left the release `pending-install`. | For a first install: `helm uninstall litellm -n llm`, then `uv run llm-bonsai setup litellm`. |
| `helm upgrade`: `conflict with "kubectl-set"` (or another manager) | Helm 4 uses server-side apply, and a field was changed by hand with `kubectl`. | Undo the manual change, or run the `helm upgrade` once with `--force-conflicts` to hand the field back to Helm. |
| Embeddings return 400 `encoding_format ... not supported by ollama` | The OpenAI SDK's default base64 encoding. | Keep `litellm_settings.drop_params: true`, or pass `encoding_format="float"` from the client. |
| Langfuse chart: `ClickHouse operator CRDs not found` | The operator (and cert-manager) must be installed before the chart. | `uv run llm-bonsai setup cert-manager`, `setup clickhouse-operator`, then `setup langfuse`. |
| After `make down` + `make up`, ClickHouse crash-loops with `Coordination error: Not authenticated` (and `langfuse-web` with it) | Old data came back: minikube's volume provisioner only deletes volumes it created since its last restart, and a new claim with the same name reuses the old directory. Keeper still has the old ClickHouse credentials. | `make down` now deletes leftover volumes and their data; run it again, then `make up`. |
| `langfuse-web` in `CrashLoopBackOff` right after install, logs say `lookup langfuse-clickhouse-headless ... no such host` | ClickHouse isn't up yet. | Wait 1–2 minutes. If it persists, check `kubectl get clickhousecluster,keepercluster -n observability`. |
| No traces in Langfuse | Wrong callback, wrong host, or wrong keys. | Use `callbacks: ["langfuse_otel"]`; check `LANGFUSE_OTEL_HOST` points at `langfuse-web.observability.svc.cluster.local:3000` and that the `litellm-langfuse` Secret matches `.env`. |
| `GET /api/public/traces` says "not available ... v4 events_only mode" | Expected in Langfuse v4. | Use `/api/public/v2/observations`. |
| `could not port-forward svc/...` | The cluster isn't running, or the lab isn't installed. | `minikube start`, or `make up`. |
| A probe says `the gateway didn't answer` | The gateway pod isn't ready (restarting, or still starting after an upgrade). The CLI already reconnects its port-forward when a pod goes away. | `kubectl -n llm get pods`, and the gateway's logs. |
| `probe replicas` was killed (Ctrl-C twice, closed terminal) and 2 replicas are left running | The probe restores the gateway on errors and a single Ctrl-C, but not if it's killed outright. | `uv run llm-bonsai setup litellm` restores the normal single-replica config. |
| `connection refused` on `localhost:4000` or `:3000` | Nothing forwards those ports unless `make ui` is running. | Run `make ui` (it reconnects by itself after a pod restart). |
| `make ui`: `port 3000 ... already in use` | Something else holds the port, often a leftover `kubectl port-forward`. | `pkill -f 'port-forward svc/'`, then `make ui` again. |
| `probe app` / `probe eval`: `NotFoundError` from Langfuse | Langfuse was reset (e.g. by `make down`) while a run was starting. | Re-run: the probes create the prompt and dataset when they're missing. |
| `kubectl top` shows tiny or old numbers | metrics-server samples lag by up to a minute. | Wait a minute and check again. |
| Odd `kubectl` errors or warnings | The `kubectl` client is more than one minor version away from the cluster. | `brew install kubernetes-cli` and check that `which kubectl` points to Homebrew. |

## Security notes

This is a local lab. All credentials are random, dev-only values in a gitignored `.env`. Nothing is exposed beyond `localhost` (only `kubectl port-forward`, opened per command), and Valkey runs without auth inside the cluster. No real provider keys are needed or used. A cloud model would only ever appear as a commented-out entry in `infra/litellm/values.yaml` reading its key from the environment.

## Pinned versions

| Component | Version |
|---|---|
| Kubernetes (minikube 1.39) | 1.37.0 |
| Ollama chart / image | 1.96.0 / 0.40.17 |
| LiteLLM chart / image | 1.111.0 / v1.111.0 |
| Postgres | 16.16-alpine |
| Valkey | 9.2.1-alpine |
| cert-manager chart | v1.21.3 |
| ClickHouse operator chart | 0.0.8 |
| Langfuse chart / app | 2.3.0 / 4.83.0 |
| ClickHouse / Keeper | 26.8.36.3 |
| Langfuse Python SDK | 5.2.0 |
| Prometheus (optional) | v3.16.0 |

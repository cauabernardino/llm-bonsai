PY := uv run python

OLLAMA_CHART_VERSION := 1.96.0
LITELLM_CHART_VERSION := 1.111.0

# Loads .env into the recipe's shell; use as a prefix: `$(LOAD_ENV) && cmd`.
LOAD_ENV := set -a && . ./.env && set +a
# Renders a Secret client-side and applies it, so re-runs update instead of failing.
APPLY := kubectl apply -f -

.PHONY: preflight cluster nuke up down namespaces secrets ollama postgres valkey litellm \
	pf bootstrap smoke

preflight:
	$(PY) scripts/preflight.py

# Docker Desktop's VM needs >= 17 GiB: minikube's 14 GiB plus headroom for the VM itself.
cluster:
	minikube start --driver=docker --cpus=6 --memory=14g --disk-size=40g
	minikube addons enable metrics-server

nuke:
	minikube delete

up: namespaces secrets ollama postgres valkey litellm

down:
	-helm uninstall litellm -n llm
	-helm uninstall ollama -n llm
	-kubectl delete -f infra/valkey/valkey.yaml -f infra/postgres/postgres.yaml
	-kubectl delete pvc --all -n llm

namespaces:
	kubectl apply -f infra/namespaces.yaml

secrets:
	$(PY) scripts/gen_secrets.py
	@$(LOAD_ENV) && \
	kubectl -n llm create secret generic litellm-master-key \
		--from-literal=master-key="$$LITELLM_MASTER_KEY" --dry-run=client -o yaml | $(APPLY) && \
	kubectl -n llm create secret generic litellm-db \
		--from-literal=username=litellm --from-literal=password="$$LITELLM_DB_PASSWORD" \
		--dry-run=client -o yaml | $(APPLY)

ollama:
	helm upgrade --install ollama ollama --repo https://helm.otwld.com/ \
		--version $(OLLAMA_CHART_VERSION) -n llm -f infra/ollama/values.yaml
	$(PY) scripts/wait_ready.py llm --timeout 900

postgres:
	kubectl apply -f infra/postgres/postgres.yaml
	kubectl -n llm rollout status statefulset/postgres --timeout=180s

valkey:
	kubectl apply -f infra/valkey/valkey.yaml
	kubectl -n llm rollout status deployment/valkey --timeout=120s

# The chart's migrations Job is a pre-install hook, so Postgres must be up first.
litellm:
	helm upgrade --install litellm oci://ghcr.io/berriai/litellm/chart/litellm \
		--version $(LITELLM_CHART_VERSION) -n llm -f infra/litellm/values.yaml --wait --timeout 10m
	$(PY) scripts/wait_ready.py llm

# Port-forwards that restart themselves if the connection drops (e.g. pod restart).
pf:
	@trap 'kill 0' INT TERM EXIT; \
	(while true; do kubectl -n llm port-forward svc/litellm 4000:4000; sleep 1; done) & \
	wait

bootstrap:
	$(PY) scripts/bootstrap_gateway.py

smoke:
	$(PY) scripts/smoke_test.py

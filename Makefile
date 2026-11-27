PY := uv run python

OLLAMA_CHART_VERSION := 1.96.0
LITELLM_CHART_VERSION := 1.111.0
CERT_MANAGER_CHART_VERSION := v1.21.3
CLICKHOUSE_OPERATOR_CHART_VERSION := 0.0.8
LANGFUSE_CHART_VERSION := 2.3.0

# Loads .env into the recipe's shell; use as a prefix: `$(LOAD_ENV) && cmd`.
LOAD_ENV := set -a && . ./.env && set +a
# Renders a Secret client-side and applies it, so re-runs update instead of failing.
APPLY := kubectl apply -f -

.PHONY: preflight cluster nuke up down namespaces secrets ollama postgres valkey litellm \
	cert-manager clickhouse-operator langfuse pf pf-stop bootstrap smoke \
	demo-fallback demo-limits demo-cache demo-replicas demo-chaos load

preflight:
	$(PY) scripts/preflight.py

# Docker Desktop's VM needs >= 17 GiB: minikube's 14 GiB plus headroom for the VM itself.
cluster:
	minikube start --driver=docker --cpus=6 --memory=14g --disk-size=40g
	minikube addons enable metrics-server

nuke:
	minikube delete

# Langfuse goes before LiteLLM so the gateway's first requests are already traced.
up: namespaces secrets ollama postgres valkey cert-manager clickhouse-operator langfuse litellm

# Keeps cert-manager and the ClickHouse operator: they're cluster-wide, install-once prerequisites.
down:
	-helm uninstall langfuse -n observability --wait
	-kubectl delete pvc --all -n observability
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
		--dry-run=client -o yaml | $(APPLY) && \
	kubectl -n llm create secret generic litellm-langfuse \
		--from-literal=LANGFUSE_PUBLIC_KEY="$$LANGFUSE_PUBLIC_KEY" \
		--from-literal=LANGFUSE_SECRET_KEY="$$LANGFUSE_SECRET_KEY" \
		--dry-run=client -o yaml | $(APPLY) && \
	kubectl -n observability create secret generic langfuse \
		--from-literal=salt="$$LANGFUSE_SALT" \
		--from-literal=encryption-key="$$LANGFUSE_ENCRYPTION_KEY" \
		--from-literal=nextauth-secret="$$LANGFUSE_NEXTAUTH_SECRET" \
		--from-literal=postgresql-password="$$LANGFUSE_DB_PASSWORD" \
		--from-literal=POSTGRES_USER=postgres \
		--from-literal=POSTGRES_PASSWORD="$$LANGFUSE_DB_PASSWORD" \
		--from-literal=POSTGRES_DB=langfuse \
		--from-literal=USERDB_USER=langfuse \
		--from-literal=USERDB_PASSWORD="$$LANGFUSE_DB_PASSWORD" \
		--from-literal=clickhouse-password="$$LANGFUSE_CLICKHOUSE_PASSWORD" \
		--from-literal=redis-password="$$LANGFUSE_REDIS_PASSWORD" \
		--from-literal=default="$$LANGFUSE_REDIS_PASSWORD" \
		--from-literal=s3-user=langfuse \
		--from-literal=s3-password="$$LANGFUSE_S3_PASSWORD" \
		--from-literal=seaweedfs_s3_config='{"identities":[{"name":"langfuse","credentials":[{"accessKey":"langfuse","secretKey":"'"$$LANGFUSE_S3_PASSWORD"'"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' \
		--from-literal=init-public-key="$$LANGFUSE_PUBLIC_KEY" \
		--from-literal=init-secret-key="$$LANGFUSE_SECRET_KEY" \
		--from-literal=init-user-email="$$LANGFUSE_INIT_USER_EMAIL" \
		--from-literal=init-user-password="$$LANGFUSE_INIT_USER_PASSWORD" \
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
# LITELLM_ARGS adds one-off overrides, e.g. `make litellm LITELLM_ARGS="--set gateway.replicaCount=2"`
# (scripts/demo_replicas.py uses it to switch shared state off and on).
litellm:
	helm upgrade --install litellm oci://ghcr.io/berriai/litellm/chart/litellm \
		--version $(LITELLM_CHART_VERSION) -n llm -f infra/litellm/values.yaml --wait --timeout 10m \
		$(LITELLM_ARGS)
	$(PY) scripts/wait_ready.py llm

# Cluster-wide prerequisites of the Langfuse chart: the ClickHouse operator renders
# ClickHouseCluster/KeeperCluster CRs and gets its webhook certificates from cert-manager.
cert-manager:
	helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
		--version $(CERT_MANAGER_CHART_VERSION) -n cert-manager --create-namespace \
		-f infra/cert-manager/values.yaml --wait
	kubectl wait --for=condition=Established --timeout=120s \
		crd/certificates.cert-manager.io crd/issuers.cert-manager.io

clickhouse-operator:
	helm upgrade --install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm \
		--version $(CLICKHOUSE_OPERATOR_CHART_VERSION) -n clickhouse-operator --create-namespace --wait
	kubectl wait --for=condition=Established --timeout=120s \
		crd/clickhouseclusters.clickhouse.com crd/keeperclusters.clickhouse.com

# Release name must be `langfuse`: service names (langfuse-web, ...) derive from it.
langfuse:
	helm upgrade --install langfuse langfuse --repo https://langfuse.github.io/langfuse-k8s \
		--version $(LANGFUSE_CHART_VERSION) -n observability -f infra/langfuse/values.yaml
	$(PY) scripts/wait_ready.py observability --timeout 900

# Port-forwards that restart themselves if the connection drops (e.g. pod restart).
pf:
	@trap 'kill 0' INT TERM EXIT; \
	(while true; do kubectl -n llm port-forward svc/litellm 4000:4000; sleep 1; done) & \
	(while true; do kubectl -n observability port-forward svc/langfuse-web 3000:3000; sleep 1; done) & \
	wait

# Kills the `make pf` loops (matched by their command line) along with their kubectl children,
# so nothing respawns. The [c] keeps the pattern from matching this recipe's own shell.
pf-stop:
	@pkill -f 'port-forward sv[c]/' && echo "port-forwards stopped" || echo "no port-forwards running"

bootstrap:
	$(PY) scripts/bootstrap_gateway.py

smoke:
	$(PY) scripts/smoke_test.py

# Phase 4 demos: each prints a "What you just saw" summary. All need `make pf` running.
demo-fallback:
	$(PY) scripts/demo_fallback.py

demo-limits:
	$(PY) scripts/demo_limits.py

demo-cache:
	$(PY) scripts/demo_cache.py

# Redeploys the gateway twice (2 replicas without, then with Valkey) and restores 1 replica.
demo-replicas:
	$(PY) scripts/demo_replicas.py

demo-chaos:
	$(PY) scripts/demo_chaos.py

load:
	$(PY) scripts/load_test.py --concurrency 1,2,4,8 --duration 20

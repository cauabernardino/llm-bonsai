PY := uv run python

OLLAMA_CHART_VERSION := 1.96.0

.PHONY: preflight cluster nuke up namespaces ollama

preflight:
	$(PY) scripts/preflight.py

# Docker Desktop's VM must have >= 14 GiB for this to fit.
cluster:
	minikube start --driver=docker --cpus=6 --memory=14g --disk-size=40g
	minikube addons enable metrics-server

nuke:
	minikube delete

up: namespaces ollama

namespaces:
	kubectl apply -f infra/namespaces.yaml

ollama:
	helm upgrade --install ollama ollama --repo https://helm.otwld.com/ \
		--version $(OLLAMA_CHART_VERSION) -n llm -f infra/ollama/values.yaml
	$(PY) scripts/wait_ready.py llm --timeout 900

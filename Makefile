PY := uv run python

.PHONY: preflight cluster nuke

preflight:
	$(PY) scripts/preflight.py

# Docker Desktop's VM must have >= 14 GiB for this to fit.
cluster:
	minikube start --driver=docker --cpus=6 --memory=14g --disk-size=40g
	minikube addons enable metrics-server

nuke:
	minikube delete

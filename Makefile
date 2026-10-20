# Lifeboat - run `make help` for the list. Quick start on a laptop (Docker + k3d + kubectl + helm):
#   make cluster import deploy monitoring workloads demo
CLUSTER  ?= lifeboat
SPEED    ?= 10
SCENARIO ?= data/scenario_flash_flood.jsonl
S3_MODE  ?= $(shell grep -s '^S3_MODE=' .env | cut -d= -f2)
PLATFORM ?= linux/amd64
export CLUSTER

.PHONY: help test itest images cluster cluster-down import k3s-import secrets deploy workloads wait \
        monitoring restart replay load demo reset status undeploy scenario lint

help:   ## list targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t 16

test:   ## unit tests (no cluster needed)
	python -m pytest -q

itest:  ## cluster check: Lease, TriagePlan, takeover, revert, RBAC (needs `make deploy workloads`)
	bash scripts/itest.sh

lint:   ## same manifest/script checks as CI (needs yamllint, kubeconform, shellcheck)
	yamllint -c .yamllint deploy .github
	kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version 1.31.0 deploy deploy/jobs deploy/monitoring
	shellcheck scripts/*.sh scripts/*/*.sh

images: ## build the three images
	docker build --platform $(PLATFORM) -t lifeboat/controller:latest .
	docker build --platform $(PLATFORM) -t lifeboat/webapp:latest workloads/webapp
	docker build --platform $(PLATFORM) -t lifeboat/worker:latest -f workloads/worker/Dockerfile .

cluster: ## laptop: create a 3-server k3d cluster (embedded etcd), 2 CPUs per node
	bash scripts/cluster/k3d-up.sh $(CLUSTER)

cluster-down: ## laptop: delete the k3d cluster
	k3d cluster delete $(CLUSTER)

import: images ## copy images onto every node (k3d, NODES="user@ip ..." over ssh, or local k3s)
	bash scripts/cluster/import-images.sh

k3s-import: import

secrets: ## create lifeboat-secrets + lifeboat-s3 from .env (generated with random keys if missing)
	bash scripts/make-secrets.sh

deploy: ## namespaces, CRD, RBAC, secrets, checkpoint store, Lifeboat controller
	kubectl apply -f deploy/00-namespaces-crd.yaml -f deploy/01-priorityclasses.yaml -f deploy/02-rbac.yaml
	bash scripts/make-secrets.sh
	@if [ "$(S3_MODE)" != "aws" ]; then kubectl apply -f deploy/05-checkpoint-store.yaml; fi
	kubectl apply -f deploy/03-lifeboat.yaml

workloads: ## tiered demo workloads (C1-C4 + placeholders)
	kubectl apply -f deploy/04-demo-workloads.yaml

wait: ## wait until everything deployed is running
	@if kubectl -n lifeboat get deploy minio >/dev/null 2>&1; then kubectl -n lifeboat rollout status deploy/minio --timeout=180s; fi
	kubectl -n lifeboat rollout status deploy/lifeboat --timeout=180s
	kubectl -n lifeboat-demo rollout status deploy/c1-emergency --timeout=180s
	kubectl -n lifeboat-demo rollout status deploy/c2-portal --timeout=180s

monitoring: ## Prometheus + Grafana (kube-prometheus-stack) and the PodMonitors
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update prometheus-community
	helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
	  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false --wait --timeout 10m
	kubectl apply -f deploy/monitoring/podmonitors.yaml

restart: ## restart pods so they pick up newly imported images
	kubectl -n lifeboat rollout restart deploy/lifeboat
	kubectl -n lifeboat-demo rollout restart deploy/c1-emergency deploy/c2-portal

replay: ## replay SCENARIO at SPEED x from inside the cluster
	kubectl -n lifeboat delete job replay --ignore-not-found --wait=true
	sed -e 's|__SPEED__|$(SPEED)|' -e 's|__SCENARIO__|$(SCENARIO)|' deploy/jobs/replay.yaml | kubectl create -f -

load: ## start the 20x Locust surge from inside the cluster (10 min)
	kubectl -n lifeboat delete job locust --ignore-not-found --wait=true
	kubectl -n lifeboat create configmap locustfile --from-file=loadtest/locustfile.py --dry-run=client -o yaml | kubectl apply -f -
	kubectl create -f deploy/jobs/locust.yaml

demo: deploy workloads wait load replay ## deploy everything, then start the surge and the flood replay together
	@echo
	@echo "Demo running. Watch it with:"
	@echo "  kubectl -n lifeboat get triageplans -w"
	@echo "  kubectl -n lifeboat logs -f job/replay"
	@echo "  make status"

reset: ## back to a clean L0 state between runs (plan, Lease, workloads, checkpoints)
	bash scripts/reset.sh

status: ## one-screen view of the cluster and Lifeboat
	@kubectl get nodes -o wide
	@echo; kubectl -n lifeboat get pods,lease,triageplans
	@echo; kubectl -n lifeboat-demo get deploy,jobs,pods

scenario:
	python scripts/make_scenario.py

undeploy: ## remove Lifeboat and the demo workloads (keeps the cluster and monitoring)
	kubectl -n lifeboat delete job replay locust --ignore-not-found
	kubectl delete -f deploy/04-demo-workloads.yaml -f deploy/03-lifeboat.yaml --ignore-not-found
	kubectl delete -f deploy/05-checkpoint-store.yaml --ignore-not-found

# Lifeboat - run `make help` for the list. Quick start on a laptop (Docker + k3d + kubectl + helm):
#   make up demo
CLUSTER  ?= lifeboat
SPEED    ?= 10
SCENARIO ?= data/scenario_flash_flood.jsonl
STAGES   ?= 150:10,330:200,360:10
SETUP    ?= all
RUNS     ?= 3
S3_MODE  ?= $(shell grep -s '^S3_MODE=' .env | cut -d= -f2)
PLATFORM ?= linux/amd64
# kube-prometheus-stack chart version. Pin it to what the AWS cluster runs (`helm -n monitoring list`).
KPS_VERSION ?=
export CLUSTER

.PHONY: help up test itest images cluster cluster-down import k3s-import secrets deploy feeds feeds-off workloads wait \
        monitoring restart replay load demo reset status undeploy scenario lint experiment

help:   ## list targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t 16

up:     ## laptop, one command: k3d cluster, images, Lifeboat, monitoring, demo workloads, wait
	$(MAKE) cluster import deploy monitoring workloads wait

test:   ## unit tests (no cluster needed)
	python -m pytest -q

itest:  ## cluster check: Lease, TriagePlan, takeover, revert, RBAC (needs `make deploy workloads`)
	bash scripts/itest.sh

lint:   ## same manifest/script checks as CI (needs yamllint, kubeconform, shellcheck)
	yamllint -c .yamllint deploy .github
	kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version 1.31.0 deploy deploy/jobs deploy/monitoring deploy/feeds deploy/baselines
	shellcheck scripts/*.sh scripts/*/*.sh

images: ## build the three images (the live feeds run from the controller image)
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

feeds:  ## live data.gov.sg feeds: CronJobs pub-feed (S1, every 2 min) and nea-feed (S2, every 5 min)
	kubectl apply -f deploy/feeds/feeds-cronjobs.yaml
	@echo "Live feeds on. Run 'make feeds-off' before any replay or experiment (make replay/reset do it)."

feeds-off: ## stop the live feeds (they would override replayed S1/S2 readings)
	kubectl -n lifeboat delete cronjob pub-feed nea-feed --ignore-not-found --cascade=foreground --wait=true

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
	  $(if $(KPS_VERSION),--version $(KPS_VERSION)) \
	  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false --wait --timeout 10m
	kubectl apply -f deploy/monitoring/podmonitors.yaml -f deploy/monitoring/grafana-dashboard.yaml \
	  -f deploy/monitoring/alert-rules.yaml

restart: ## restart pods so they pick up newly imported images
	kubectl -n lifeboat rollout restart deploy/lifeboat
	kubectl -n lifeboat-demo rollout restart deploy/c1-emergency deploy/c2-portal

replay: feeds-off ## replay SCENARIO at SPEED x from inside the cluster (stops live feeds first)
	kubectl -n lifeboat delete job replay --ignore-not-found --wait=true
	sed -e 's|__SPEED__|$(SPEED)|' -e 's|__SCENARIO__|$(SCENARIO)|' deploy/jobs/replay.yaml | kubectl create -f -

load: ## start the 20x Locust surge from inside the cluster (STAGES, default 6 min)
	kubectl -n lifeboat delete job locust --ignore-not-found --wait=true
	kubectl -n lifeboat create configmap locustfile --from-file=loadtest/locustfile.py --dry-run=client -o yaml | kubectl apply -f -
	sed -e 's|__STAGES__|$(STAGES)|' deploy/jobs/locust.yaml | kubectl create -f -

demo: deploy workloads wait load ## deploy, then the surge plus the experiment warnings at speed 1 (alert leads the surge by 60 s)
	$(MAKE) replay SCENARIO=data/scenario_experiment.jsonl SPEED=1
	@echo
	@echo "Demo running (about 8 min): L2 at t=90 s, surge t=150-330 s, back to L0 after the calm period."
	@echo "For the video, shorten the calm period first: kubectl -n lifeboat set env deploy/lifeboat LIFEBOAT_CALM_MINUTES=1"
	@echo "Watch it with:"
	@echo "  kubectl -n lifeboat get triageplans -w"
	@echo "  kubectl -n lifeboat logs -f job/replay"
	@echo "  make status"

reset: feeds-off ## back to a clean L0 state between runs (plan, Lease, workloads, checkpoints)
	bash scripts/reset.sh

status: ## one-screen view of the cluster and Lifeboat
	@kubectl get nodes -o wide
	@echo; kubectl -n lifeboat get pods,lease,triageplans
	@echo; kubectl -n lifeboat-demo get deploy,jobs,pods

scenario: ## regenerate the replay scenarios in data/
	python scripts/make_scenario.py

experiment: ## measure E1/E2/E3 for SETUP (B0 B1 B2 lifeboat or all) x RUNS on the cluster
	python3 scripts/run_experiment.py --setup $(SETUP) --runs $(RUNS)

undeploy: feeds-off ## remove Lifeboat and the demo workloads (keeps the cluster and monitoring)
	kubectl -n lifeboat delete job replay locust --ignore-not-found
	kubectl delete -f deploy/04-demo-workloads.yaml -f deploy/03-lifeboat.yaml --ignore-not-found
	kubectl delete -f deploy/05-checkpoint-store.yaml --ignore-not-found

#!/usr/bin/env bash
# Install OpenFaaS (faas-netes + cron-connector) on k3s via Helm, sync the HMAC secret
# into `openfaas-fn`, and deploy the PUB (S1, 2-min cron) and NEA (S2, 5-min cron) feeds
# from `feeds/stack.yml` / `deploy/feeds/openfaas-functions.yaml`.
set -euo pipefail
cd "$(dirname "$0")/.."

bash scripts/make-secrets.sh

kubectl create namespace openfaas --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create namespace openfaas-fn --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Sync LIFEBOAT_HMAC_SECRET into openfaas-fn so OpenFaaS mounts /var/openfaas/secrets/LIFEBOAT_HMAC_SECRET
ENV_FILE="${ENV_FILE:-.env}"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
kubectl create secret generic lifeboat-secrets -n openfaas-fn \
  --from-literal=LIFEBOAT_HMAC_SECRET="$LIFEBOAT_HMAC_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "secret openfaas-fn/lifeboat-secrets applied"

helm repo add openfaas https://openfaas.github.io/faas-netes/
helm repo update openfaas
helm upgrade --install openfaas openfaas/openfaas -n openfaas \
  --set functionNamespace=openfaas-fn \
  --set generateBasicAuth=true \
  --set operator.create=true \
  --wait --timeout 5m
helm upgrade --install cron-connector openfaas/cron-connector -n openfaas \
  --wait --timeout 5m

kubectl apply -f deploy/feeds/openfaas-functions.yaml
kubectl -n openfaas-fn rollout status deploy/pub-feed --timeout=180s || true
kubectl -n openfaas-fn rollout status deploy/nea-feed --timeout=180s || true
echo "OpenFaaS feeds (pub-feed */2m, nea-feed */5m) deployed in openfaas-fn."


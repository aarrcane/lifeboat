#!/usr/bin/env bash
# Put the cluster back to a clean L0 state between runs (make reset):
#   stop Lifeboat -> forget the plan and the Lease -> fresh demo workloads -> wipe checkpoints -> start Lifeboat
# Lifeboat keeps its voter state in memory, so restarting it also clears the alert level.
set -euo pipefail

SYS=lifeboat
APP=lifeboat-demo
LIFEBOAT_REPLICAS="${LIFEBOAT_REPLICAS:-2}"

echo "1/6 stopping Lifeboat"
kubectl -n "$SYS" scale deploy/lifeboat --replicas=0
kubectl -n "$SYS" wait --for=delete pod -l app=lifeboat --timeout=90s 2>/dev/null || true

echo "2/6 removing replay/load jobs, the TriagePlan and the executor Lease"
kubectl -n "$SYS" delete job replay locust --ignore-not-found --wait=true
kubectl -n "$SYS" delete triageplans.lifeboat.sit.edu --all --ignore-not-found
kubectl -n "$SYS" delete lease lifeboat-executor --ignore-not-found

echo "3/6 removing demo workloads"
kubectl delete -f deploy/04-demo-workloads.yaml --ignore-not-found --wait=true
kubectl -n "$APP" wait --for=delete pod --all --timeout=120s 2>/dev/null || true

echo "4/6 wiping checkpoints"
if kubectl -n "$SYS" get deploy minio >/dev/null 2>&1; then
  kubectl -n "$SYS" rollout restart deploy/minio          # emptyDir: a new pod starts empty
  kubectl -n "$SYS" rollout status deploy/minio --timeout=120s
else
  bucket="$(kubectl -n "$SYS" get secret lifeboat-s3 -o jsonpath='{.data.LIFEBOAT_S3_BUCKET}' | base64 -d)"
  echo "   no MinIO: emptying s3://$bucket"
  aws s3 rm "s3://$bucket" --recursive --only-show-errors
fi

echo "5/6 starting demo workloads"
kubectl apply -f deploy/04-demo-workloads.yaml >/dev/null
kubectl -n "$APP" rollout status deploy/c1-emergency --timeout=120s

echo "6/6 starting Lifeboat"
kubectl -n "$SYS" scale deploy/lifeboat --replicas="$LIFEBOAT_REPLICAS"
kubectl -n "$SYS" rollout status deploy/lifeboat --timeout=180s
echo "Reset done: L0, no plan, takeover numbers start again at 1."

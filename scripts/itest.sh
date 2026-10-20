#!/usr/bin/env bash
# Run tests/integration/cluster_check.py against the current kubectl cluster (make itest).
# It acts as the `lifeboat` ServiceAccount, so missing RBAC shows up as a failure.
# Lifeboat pods are scaled to 0 for the run (they would fight over the Lease) and restored after.
set -euo pipefail
cd "$(dirname "$0")/.."

ADMIN_KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export ADMIN_KUBECONFIG
tmp="$(mktemp -d)"
replicas="$(kubectl -n lifeboat get deploy lifeboat -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"

cleanup() {
  kubectl -n lifeboat delete lease lifeboat-executor --ignore-not-found >/dev/null
  kubectl -n lifeboat delete triageplans.lifeboat.sit.edu --all --ignore-not-found >/dev/null
  if [[ "$replicas" != "0" ]]; then kubectl -n lifeboat scale deploy/lifeboat --replicas="$replicas" >/dev/null; fi
  rm -rf "$tmp"
}
trap cleanup EXIT

if [[ "$replicas" != "0" ]]; then
  kubectl -n lifeboat scale deploy/lifeboat --replicas=0 >/dev/null
  kubectl -n lifeboat wait --for=delete pod -l app=lifeboat --timeout=90s >/dev/null 2>&1 || true
fi
kubectl -n lifeboat delete lease lifeboat-executor --ignore-not-found >/dev/null
kubectl -n lifeboat delete triageplans.lifeboat.sit.edu --all --ignore-not-found >/dev/null

# kubeconfig for the lifeboat ServiceAccount, same API server and CA as the admin one
server="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > "$tmp/ca.crt"
token="$(kubectl -n lifeboat create token lifeboat --duration=1h)"
cat > "$tmp/sa.kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters: [{name: c, cluster: {server: "$server", certificate-authority: "$tmp/ca.crt"}}]
users: [{name: lifeboat, user: {token: "$token"}}]
contexts: [{name: sa, context: {cluster: c, user: lifeboat}}]
current-context: sa
EOF

KUBECONFIG="$tmp/sa.kubeconfig" python3 tests/integration/cluster_check.py

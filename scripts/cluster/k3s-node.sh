#!/usr/bin/env bash
# Install k3s on a Linux machine (VM, spare PC or EC2) as one of 3 servers with embedded etcd.
# Run as root (sudo). Every server gets the same K3S_TOKEN.
#
#   First server:   sudo K3S_TOKEN=<token> TLS_SAN=<public ip or dns> scripts/cluster/k3s-node.sh first
#   Other servers:  sudo K3S_TOKEN=<token> FIRST=<first server's private ip> scripts/cluster/k3s-node.sh join
#
# Generate a token once:  python3 -c 'import secrets; print(secrets.token_hex(24))'
set -euo pipefail

ROLE="${1:?usage: k3s-node.sh first|join}"
: "${K3S_TOKEN:?set K3S_TOKEN (same value on all servers)}"
export INSTALL_K3S_VERSION="${INSTALL_K3S_VERSION:-v1.31.4+k3s1}"
export K3S_TOKEN

args=(server --disable traefik --write-kubeconfig-mode 644 --secrets-encryption)
[[ -n "${TLS_SAN:-}" ]] && args+=(--tls-san "$TLS_SAN")

case "$ROLE" in
  first) args+=(--cluster-init) ;;
  join)  : "${FIRST:?set FIRST to the private IP of the first server}"; args+=(--server "https://${FIRST}:6443") ;;
  *)     echo "role must be first or join" >&2; exit 1 ;;
esac

curl -sfL https://get.k3s.io | sh -s - "${args[@]}"
echo "k3s $ROLE server installed. Check with: sudo k3s kubectl get nodes"

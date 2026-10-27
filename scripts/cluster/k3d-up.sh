#!/usr/bin/env bash
# Laptop cluster: 3 k3s servers with embedded etcd, running as Docker containers (k3d).
#
#   scripts/cluster/k3d-up.sh [name]          (or: make cluster)
#
# Every k3d node sees ALL of the laptop's CPUs, so 3 nodes would report 3x the real capacity
# and the cluster would never fill up (no triage to watch). We reserve CPU on each node's
# kubelet so each node offers NODE_CPUS (default 2) to pods: 6 CPUs in total, which the
# 20x surge overflows on purpose.
set -euo pipefail

NAME="${1:-${CLUSTER:-lifeboat}}"
NODE_CPUS="${NODE_CPUS:-2}"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.31.4-k3s1}"

command -v docker >/dev/null || { echo "Docker is not installed or not running." >&2; exit 1; }
command -v k3d >/dev/null || { echo "k3d is not installed: https://k3d.io (or: winget install k3d / brew install k3d)" >&2; exit 1; }

HOST_CPUS="$(docker info --format '{{.NCPU}}')"
RESERVED=$(( HOST_CPUS > NODE_CPUS ? HOST_CPUS - NODE_CPUS : 0 ))
echo "Docker sees $HOST_CPUS CPUs; reserving $RESERVED per node so each node offers $NODE_CPUS."

if k3d cluster list "$NAME" >/dev/null 2>&1; then
  echo "Cluster '$NAME' already exists. Delete it first with: make cluster-down"
  exit 0
fi

k3d cluster create "$NAME" \
  --image "$K3S_IMAGE" \
  --servers 3 --agents 0 \
  --api-port 6550 \
  --k3s-arg "--disable=traefik@server:*" \
  --k3s-arg "--secrets-encryption@server:*" \
  --k3s-arg "--kubelet-arg=system-reserved=cpu=${RESERVED}@server:*" \
  --wait

kubectl config use-context "k3d-$NAME" >/dev/null
echo
kubectl get nodes -o custom-columns='NODE:.metadata.name,ROLES:.metadata.labels.node-role\.kubernetes\.io/etcd,CPU:.status.allocatable.cpu,READY:.status.conditions[-1].type'
echo
echo "Cluster ready. Next: make import deploy workloads"

#!/usr/bin/env bash
# Put the three Lifeboat images on EVERY node (imagePullPolicy is IfNotPresent and there is no
# registry, so a node without the image cannot start the pod).
#
#   k3d cluster named $CLUSTER exists  -> k3d image import (all nodes)
#   NODES="ubuntu@1.2.3.4 ubuntu@..."   -> docker save | ssh <node> sudo k3s ctr images import -
#   k3s installed on this machine       -> import into the local node only
set -euo pipefail

CLUSTER="${CLUSTER:-lifeboat}"
IMAGES=(lifeboat/controller:latest lifeboat/webapp:latest lifeboat/worker:latest)
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=accept-new}"

if command -v k3d >/dev/null && k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d image import -c "$CLUSTER" "${IMAGES[@]}"
elif [[ -n "${NODES:-}" ]]; then
  for node in $NODES; do
    echo "importing images on $node"
    # shellcheck disable=SC2086
    docker save "${IMAGES[@]}" | ssh $SSH_OPTS "$node" sudo k3s ctr images import -
  done
elif command -v k3s >/dev/null; then
  docker save "${IMAGES[@]}" | sudo k3s ctr images import -
else
  echo "No k3d cluster '$CLUSTER', no NODES list and no local k3s: nowhere to import to." >&2
  exit 1
fi
echo "Images imported. Running pods keep the old image until: make restart"

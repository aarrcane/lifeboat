#!/usr/bin/env bash
# Start the stopped servers again and refresh NODES + kubeconfig-aws.yaml.
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck disable=SC1091
source .aws-cluster.env
export AWS_DEFAULT_REGION="$REGION"
# shellcheck disable=SC2086
aws ec2 start-instances --instance-ids $INSTANCES >/dev/null
# shellcheck disable=SC2086
aws ec2 wait instance-running --instance-ids $INSTANCES
echo "Running; waiting 60 s for k3s to come back"
sleep 60
bash scripts/aws/refresh.sh
KUBECONFIG=kubeconfig-aws.yaml kubectl get nodes

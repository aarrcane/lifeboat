#!/usr/bin/env bash
# Stop (not delete) the 3 servers to save lab credit. scripts/aws/start.sh brings them back
# with the same cluster, data and Elastic IP. The Learner Lab also stops them when a session ends.
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck disable=SC1091
source .aws-cluster.env
export AWS_DEFAULT_REGION="$REGION"
# shellcheck disable=SC2086
aws ec2 stop-instances --instance-ids $INSTANCES >/dev/null
echo "Stopping $INSTANCES"

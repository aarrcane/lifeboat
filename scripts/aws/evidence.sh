#!/usr/bin/env bash
# Record what is actually running on the AWS cluster (the "AWS run" deliverable and the source
# for report section 5). Run from the laptop after `make deploy monitoring workloads wait`:
#
#   export SSH_KEY=~/labsuser.pem KUBECONFIG=$PWD/kubeconfig-aws.yaml
#   scripts/aws/evidence.sh
#
# Writes one file per command into results/aws_evidence/ (commit them).
set -uo pipefail          # no -e: one failing command must not lose the others
cd "$(dirname "$0")/../.." || exit 1
STATE=.aws-cluster.env
[[ -f "$STATE" ]] || { echo "No $STATE: run scripts/aws/up.sh first." >&2; exit 1; }
# shellcheck disable=SC1090
source "$STATE"
export AWS_DEFAULT_REGION="$REGION"
SSH_KEY="${SSH_KEY:?set SSH_KEY to the lab .pem}"
OUT=results/aws_evidence
mkdir -p "$OUT"

run() {   # file, command...
  local f="$OUT/$1"; shift
  { echo "\$ $*"; echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ)"; "$@" 2>&1; } > "$f"
  echo "  $f"
}

echo "== saving evidence to $OUT"
run nodes.txt kubectl get nodes -o wide
run version.txt kubectl version
run lifeboat-pods.txt kubectl -n lifeboat get pods -o wide
run demo-pods.txt kubectl -n lifeboat-demo get deploy,jobs,pods -o wide
run secrets-encrypt.txt ssh -i "$SSH_KEY" -p "${SSH_PORT:-22}" -o StrictHostKeyChecking=accept-new "ubuntu@$EIP" sudo k3s secrets-encrypt status
# shellcheck disable=SC2086,SC2016  # backticks are a JMESPath literal, not shell
run ec2-instances.txt aws ec2 describe-instances --instance-ids $INSTANCES --output table \
  --query 'Reservations[].Instances[].{Name:Tags[?Key==`Name`]|[0].Value,Id:InstanceId,Type:InstanceType,State:State.Name,AZ:Placement.AvailabilityZone,PrivateIp:PrivateIpAddress,PublicIp:PublicIpAddress}'
run s3-objects.txt bash -c "aws s3 ls 's3://$BUCKET' --recursive | head -n 50"
run helm.txt helm list -A
run top-nodes.txt kubectl top nodes
run top-lifeboat.txt kubectl -n lifeboat top pod
echo "done. Commit $OUT/ with the results."

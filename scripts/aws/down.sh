#!/usr/bin/env bash
# Delete everything up.sh created: instances, Elastic IP, security group and (unless
# KEEP_BUCKET=1) the S3 bucket with its checkpoints. Also works on a half-finished up.sh.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
STATE=.aws-cluster.env
[[ -f "$STATE" ]] || { echo "No $STATE: nothing to delete."; exit 0; }
# shellcheck disable=SC1090
source "$STATE"
export AWS_DEFAULT_REGION="$REGION"

if [[ -n "${INSTANCES:-}" ]]; then
  echo "== terminating $INSTANCES"
  # shellcheck disable=SC2086
  aws ec2 terminate-instances --instance-ids $INSTANCES >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-terminated --instance-ids $INSTANCES
fi
if [[ -n "${EIP_ALLOC:-}" ]]; then
  echo "== releasing Elastic IP ${EIP:-}"
  aws ec2 release-address --allocation-id "$EIP_ALLOC"
fi
if [[ -n "${SG:-}" ]]; then
  echo "== deleting security group $SG"
  for _ in 1 2 3 4 5 6; do aws ec2 delete-security-group --group-id "$SG" 2>/dev/null && break; sleep 10; done
fi
if [[ -n "${BUCKET:-}" && "${KEEP_BUCKET:-0}" != "1" ]]; then
  echo "== deleting s3://$BUCKET"
  aws s3 rb "s3://$BUCKET" --force >/dev/null
fi
rm -f "$STATE" kubeconfig-aws.yaml
echo "Done. Nothing from up.sh is left running."

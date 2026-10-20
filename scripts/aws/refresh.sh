#!/usr/bin/env bash
# Re-read the nodes' public IPs (they change on every stop/start, except server-1's Elastic IP)
# and rewrite NODES in .aws-cluster.env plus kubeconfig-aws.yaml. Run after starting the lab again.
set -euo pipefail
cd "$(dirname "$0")/../.."
STATE=.aws-cluster.env
[[ -f "$STATE" ]] || { echo "No $STATE: run scripts/aws/up.sh first." >&2; exit 1; }
# shellcheck disable=SC1090
source "$STATE"
export AWS_DEFAULT_REGION="$REGION"
SSH_KEY="${SSH_KEY:?set SSH_KEY to the lab .pem}"

# shellcheck disable=SC2086,SC2016  # backticks are a JMESPath literal, not shell
ips="$(aws ec2 describe-instances --instance-ids $INSTANCES \
        --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,PublicIpAddress]' --output text | sort | awk '{print $2}')"
NODES="$(for ip in $ips; do printf 'ubuntu@%s ' "$ip"; done)"
NODES="${NODES% }"
sed -i.bak '/^NODES=/d' "$STATE" && rm -f "$STATE.bak"
echo "NODES=\"$NODES\"" >> "$STATE"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "ubuntu@$EIP" 'sudo cat /etc/rancher/k3s/k3s.yaml' \
  | sed -e "s|https://127.0.0.1:6443|https://$EIP:6443|" -e 's|: default$|: lifeboat-aws|' > kubeconfig-aws.yaml
chmod 600 kubeconfig-aws.yaml
echo "NODES=$NODES"
echo "kubeconfig-aws.yaml points at https://$EIP:6443"

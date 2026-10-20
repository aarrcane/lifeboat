#!/usr/bin/env bash
# Final-run cluster on AWS: 3 EC2 servers running k3s with embedded etcd, plus an S3 bucket
# for checkpoints. Written for the AWS Academy Learner Lab:
#   - no IAM users (blocked): pods reach S3 through the instance profile LabInstanceProfile (LabRole)
#   - the lab's key pair `vockey`: download labsuser.pem from "AWS Details" and pass SSH_KEY
#
#   export SSH_KEY=~/labsuser.pem          # chmod 400 it first
#   scripts/aws/up.sh
#
# Settings (env): AWS_REGION (us-east-1), INSTANCE_TYPE (t3.large), KEY_NAME (vockey),
#   INSTANCE_PROFILE (LabInstanceProfile), ALLOW_CIDR (your public IP/32: who may reach ssh, the
#   Kubernetes API and NodePorts; add teammates as "a.b.c.d/32,e.f.g.h/32").
# Writes .aws-cluster.env (state for the other scripts) and kubeconfig-aws.yaml. Both gitignored.
set -euo pipefail
cd "$(dirname "$0")/../.."

export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
NAME="${NAME:-lifeboat}"
TYPE="${INSTANCE_TYPE:-t3.large}"
KEY="${KEY_NAME:-vockey}"
PROFILE="${INSTANCE_PROFILE:-LabInstanceProfile}"
SSH_KEY="${SSH_KEY:?set SSH_KEY to the .pem for key pair $KEY (Learner Lab: labsuser.pem)}"
K3S_VERSION="${INSTALL_K3S_VERSION:-v1.31.4+k3s1}"
STATE=.aws-cluster.env
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

[[ -f "$STATE" ]] && { echo "$STATE exists: a cluster is already up. Run scripts/aws/down.sh first." >&2; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "AWS credentials missing or expired. Learner Lab: Start Lab > AWS Details > copy into ~/.aws/credentials" >&2; exit 1; }
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ALLOW_CIDR="${ALLOW_CIDR:-$(curl -s https://checkip.amazonaws.com)/32}"

save() { echo "$1=\"$2\"" >> "$STATE"; }   # written as we go, so down.sh can clean up a half-finished run
save REGION "$AWS_DEFAULT_REGION"

echo "== finding Ubuntu 24.04 AMI"
AMI="$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
        --query Parameter.Value --output text 2>/dev/null || true)"
if [[ -z "$AMI" || "$AMI" == "None" ]]; then
  AMI="$(aws ec2 describe-images --owners 099720109477 \
          --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' 'Name=state,Values=available' \
          --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
fi
echo "   $AMI"

echo "== security group"
VPC="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SG="$(aws ec2 create-security-group --group-name "$NAME-k3s-$(date +%s)" --description "Lifeboat k3s cluster" \
       --vpc-id "$VPC" --query GroupId --output text)"
save SG "$SG"
aws ec2 create-tags --resources "$SG" --tags "Key=Project,Value=$NAME"
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol -1 --source-group "$SG" >/dev/null   # node to node
IFS=',' read -r -a cidrs <<< "$ALLOW_CIDR"
for cidr in "${cidrs[@]}"; do
  for port in 22 6443 30000-32767; do
    aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$port" --cidr "$cidr" >/dev/null
  done
done
echo "   $SG (ssh, API and NodePorts open to $ALLOW_CIDR)"

echo "== elastic IP for server-1 (stable API address across lab sessions)"
EIP_ALLOC="$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)"
save EIP_ALLOC "$EIP_ALLOC"
EIP="$(aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC" --query 'Addresses[0].PublicIp' --output text)"
save EIP "$EIP"
echo "   $EIP"

echo "== S3 bucket"
BUCKET="$NAME-checkpoints-$ACCOUNT"
if [[ "$AWS_DEFAULT_REGION" == "us-east-1" ]]; then
  aws s3api create-bucket --bucket "$BUCKET" >/dev/null
else
  aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$AWS_DEFAULT_REGION" >/dev/null
fi
save BUCKET "$BUCKET"
echo "   s3://$BUCKET"

TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
save TOKEN "$TOKEN"

userdata() {   # $1 = extra k3s args
  cat <<EOF
#!/bin/bash
set -eux
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=$K3S_VERSION K3S_TOKEN=$TOKEN sh -s - server \\
  --disable traefik --write-kubeconfig-mode 644 --tls-san $EIP $1
EOF
}

launch() {   # $1 = name suffix, $2 = extra k3s args
  local ud; ud="$(mktemp)"; userdata "$2" > "$ud"
  aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --key-name "$KEY" \
    --security-group-ids "$SG" --iam-instance-profile "Name=$PROFILE" \
    --metadata-options HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=2 \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3"}}]' \
    --user-data "file://$ud" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME-$1},{Key=Project,Value=$NAME}]" \
    --query 'Instances[0].InstanceId' --output text
  rm -f "$ud"
}

echo "== server-1 (cluster-init)"
I1="$(launch server-1 --cluster-init)"; save INSTANCES "$I1"
aws ec2 wait instance-running --instance-ids "$I1"
aws ec2 associate-address --instance-id "$I1" --allocation-id "$EIP_ALLOC" >/dev/null
FIRST_IP="$(aws ec2 describe-instances --instance-ids "$I1" --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)"

echo "== waiting for k3s on server-1"
for _ in $(seq 1 60); do
  if "${SSH[@]}" "ubuntu@$EIP" 'sudo k3s kubectl get --raw=/readyz' >/dev/null 2>&1; then break; fi
  sleep 10
done
"${SSH[@]}" "ubuntu@$EIP" 'sudo k3s kubectl get --raw=/readyz' >/dev/null

echo "== server-2 and server-3 (join $FIRST_IP)"
I2="$(launch server-2 "--server https://$FIRST_IP:6443")"
I3="$(launch server-3 "--server https://$FIRST_IP:6443")"
sed -i.bak '/^INSTANCES=/d' "$STATE" && rm -f "$STATE.bak"
save INSTANCES "$I1 $I2 $I3"
aws ec2 wait instance-running --instance-ids "$I2" "$I3"

echo "== waiting for 3 Ready nodes"
for _ in $(seq 1 60); do
  n="$("${SSH[@]}" "ubuntu@$EIP" "sudo k3s kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready'" || echo 0)"
  [[ "$n" -ge 3 ]] && break
  sleep 10
done
"${SSH[@]}" "ubuntu@$EIP" 'sudo k3s kubectl get nodes -o wide'

bash scripts/aws/refresh.sh

cat <<EOF

AWS cluster is up.
  export KUBECONFIG=\$PWD/kubeconfig-aws.yaml
  # in .env: S3_MODE=aws  and  LIFEBOAT_S3_BUCKET=$BUCKET
  source .aws-cluster.env && SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new" NODES="\$NODES" make import
  make deploy monitoring workloads wait
When you are done for the day: scripts/aws/stop.sh   (or scripts/aws/down.sh to delete everything)
EOF

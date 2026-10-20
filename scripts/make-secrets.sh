#!/usr/bin/env bash
# Create the cluster Secrets from a local .env (never committed).
#
#   lifeboat-secrets  (ns lifeboat)                 LIFEBOAT_HMAC_SECRET: signs readings, guards /override
#   lifeboat-s3       (ns lifeboat + lifeboat-demo) checkpoint store settings for controller and workers
#
# First run: if .env does not exist it is created with random keys (see .env.example).
# Safe to re-run: secrets are applied, not recreated.
set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"

rand() { python3 -c 'import secrets; print(secrets.token_hex(24))'; }

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No $ENV_FILE found, creating one with random keys."
  umask 077
  cat > "$ENV_FILE" <<EOF
# Local secrets for Lifeboat. Gitignored. Share with teammates privately, not in the repo.
LIFEBOAT_HMAC_SECRET=$(rand)
# minio = laptop/k3d (MinIO in the cluster); aws = real S3 via the EC2 instance profile
S3_MODE=minio
LIFEBOAT_S3_BUCKET=lifeboat-checkpoints
MINIO_ROOT_USER=lifeboat
MINIO_ROOT_PASSWORD=$(rand)
AWS_DEFAULT_REGION=us-east-1
EOF
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${LIFEBOAT_HMAC_SECRET:?missing in $ENV_FILE}"
S3_MODE="${S3_MODE:-minio}"
LIFEBOAT_S3_BUCKET="${LIFEBOAT_S3_BUCKET:-lifeboat-checkpoints}"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

apply_secret() {   # name namespace --from-literal=...
  local name="$1" ns="$2"; shift 2
  kubectl create secret generic "$name" -n "$ns" "$@" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "secret $ns/$name applied"
}

apply_secret lifeboat-secrets lifeboat --from-literal=LIFEBOAT_HMAC_SECRET="$LIFEBOAT_HMAC_SECRET"

case "$S3_MODE" in
  minio)
    : "${MINIO_ROOT_USER:?missing in $ENV_FILE}" "${MINIO_ROOT_PASSWORD:?missing in $ENV_FILE}"
    s3_args=(
      --from-literal=LIFEBOAT_CHECKPOINT_BACKEND=s3
      --from-literal=LIFEBOAT_S3_BUCKET="$LIFEBOAT_S3_BUCKET"
      --from-literal=LIFEBOAT_S3_ENDPOINT=http://minio.lifeboat.svc.cluster.local:9000
      --from-literal=AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER"
      --from-literal=AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD"
      --from-literal=AWS_DEFAULT_REGION="$AWS_DEFAULT_REGION"
    ) ;;
  aws)
    # No keys: boto3 in the pods picks up the instance profile (LabRole) through the
    # EC2 metadata service. scripts/aws/up.sh sets the hop limit to 2 so pods can reach it.
    s3_args=(
      --from-literal=LIFEBOAT_CHECKPOINT_BACKEND=s3
      --from-literal=LIFEBOAT_S3_BUCKET="$LIFEBOAT_S3_BUCKET"
      --from-literal=LIFEBOAT_S3_ENDPOINT=
      --from-literal=AWS_DEFAULT_REGION="$AWS_DEFAULT_REGION"
    ) ;;
  *) echo "S3_MODE must be minio or aws (got '$S3_MODE')" >&2; exit 1 ;;
esac

for ns in lifeboat lifeboat-demo; do
  apply_secret lifeboat-s3 "$ns" "${s3_args[@]}"
done

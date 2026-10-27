# Running Lifeboat on a cluster

Two ways, same commands after the cluster exists:

| | **A. Laptop (k3d)** | **B. AWS (final run)** |
|---|---|---|
| Who | everyone, from day one | P4 runs it; the team shares it |
| What | 3 k3s servers with embedded etcd, as Docker containers | 3 EC2 servers with k3s + embedded etcd, S3 for checkpoints |
| Cost | free | about US$0.26/h while running (3 x t3.large + Elastic IP) from Learner Lab credit |
| Create | `make cluster` | `scripts/aws/up.sh` |

Both are real 3-server clusters: etcd keeps working when one server dies (2 of 3 is a quorum),
which is challenge 4 in the proposal.

---

## A. Laptop cluster (k3d)

### 1. Install the tools (once)

You need Docker, k3d, kubectl, helm, make and Python 3.

**Windows:** do everything inside **WSL2 Ubuntu** (the scripts are bash).
1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/), then Settings → Resources → WSL integration → turn on Ubuntu. Give Docker at least **4 CPUs and 8 GB RAM** (Prometheus + 3 servers are heavy).
2. In the Ubuntu terminal:
   ```bash
   sudo apt update && sudo apt install -y make python3-pip git
   curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
   sudo snap install kubectl --classic
   curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
   ```

**macOS:** `brew install k3d kubectl helm` plus Docker Desktop (same CPU/RAM settings).

Check: `docker info`, `k3d version`, `kubectl version --client`, `helm version`.

### 2. Bring everything up

```bash
git clone https://github.com/aarrcane/lifeboat && cd lifeboat
pip install -r requirements-dev.txt
make cluster          # 3 servers, ~1 min
make import           # build the 3 images and copy them onto every server
make deploy           # namespaces, CRD, RBAC, secrets (.env made for you), MinIO, Lifeboat x2
make monitoring       # Prometheus + Grafana, ~5 min the first time
make workloads        # C1-C4 demo tiers + placeholders
make wait             # until it is all running
make itest            # Lease / TriagePlan / takeover / revert check: should end in ALL PASSED
make status
```

### 3. Day-to-day

| I want to... | Run |
|---|---|
| see what is running | `make status` |
| run the flood demo (surge + replay) | `make demo`, then `kubectl -n lifeboat get triageplans -w` |
| replay faster or another scenario | `make replay SPEED=30 SCENARIO=data/my.jsonl` |
| start the surge only | `make load` |
| go back to a clean L0 before the next run | `make reset` |
| use my code changes | `make import restart` |
| open Grafana | `kubectl -n monitoring port-forward svc/kps-grafana 3000:80`, then http://localhost:3000 (user `admin`; password: `kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' \| base64 -d`) |
| delete the cluster | `make cluster-down` |

### 4. Show that the cluster survives a dead server (progress demo)

```bash
kubectl get nodes                       # 3 Ready
docker stop k3d-lifeboat-server-1       # kill one server
kubectl get nodes                       # API still answers: etcd still has 2 of 3
kubectl -n lifeboat get triageplans     # the plan is still there
docker start k3d-lifeboat-server-1      # it rejoins
```

### Why each node only offers 2 CPUs

Every k3d node sees all of your laptop's CPUs, so 3 nodes would claim 3x your real capacity and
the surge would never fill the cluster (nothing for Lifeboat to triage). `make cluster` reserves
the rest on each node so each offers 2 CPUs, 6 in total. Change it with `NODE_CPUS=3 make cluster`.

---

## B. AWS cluster (Learner Lab)

1. **Start Lab**, wait for the green dot, then **AWS Details**:
   - copy the CLI credentials into `~/.aws/credentials` (they expire with the session)
   - download the **PEM** key, save it as `~/labsuser.pem`, run `chmod 400 ~/labsuser.pem`
2. Create the cluster (about 5 minutes):
   ```bash
   export SSH_KEY=~/labsuser.pem
   scripts/aws/up.sh
   ```
   Let teammates use it too by listing their IPs: `ALLOW_CIDR="1.2.3.4/32,5.6.7.8/32" scripts/aws/up.sh`.
3. Point kubectl and the checkpoints at AWS:
   ```bash
   export KUBECONFIG=$PWD/kubeconfig-aws.yaml
   # in .env: S3_MODE=aws and LIFEBOAT_S3_BUCKET=<bucket printed by up.sh>
   source .aws-cluster.env
   SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new" make import
   make deploy monitoring workloads wait itest
   ```
4. **Stop when you are done for the day**: `scripts/aws/stop.sh`. Next session: `scripts/aws/start.sh`
   (same cluster and data; it refreshes the node IPs and the kubeconfig).
5. **Delete everything** after the final run: `scripts/aws/down.sh` (add `KEEP_BUCKET=1` to keep the checkpoints).

Learner Lab notes: it cannot create IAM users, so pods reach S3 through the instance profile
`LabInstanceProfile` (up.sh allows pods to use the EC2 metadata service). Sessions end after a
few hours and stop the instances; `scripts/aws/start.sh` brings them back. Server-1 has an
Elastic IP so the Kubernetes API address never changes.

## C. Other Linux machines (VMs, spare PCs)

```bash
python3 -c 'import secrets; print(secrets.token_hex(24))'                 # one token for all 3
sudo K3S_TOKEN=<token> TLS_SAN=<address you will use> scripts/cluster/k3s-node.sh first
sudo K3S_TOKEN=<token> FIRST=<private IP of the first> scripts/cluster/k3s-node.sh join   # on the other two
```
Then copy `/etc/rancher/k3s/k3s.yaml` from the first server, replace `127.0.0.1` with its address,
and use `NODES="user@ip1 user@ip2 user@ip3" make import`.

---

## Secrets

Nothing secret is in git. `make deploy` runs `make secrets`, which reads `.env` (gitignored) and,
on the first run, creates it with random keys (`.env.example` shows the fields). Everyone who
deploys to the **same** cluster needs the **same** `.env`: share it privately, never in the repo.

Inside the cluster, Secrets are encrypted at rest: every k3s server starts with
`--secrets-encryption` (`make cluster`, `scripts/aws/up.sh`, `scripts/cluster/k3s-node.sh`), so etcd
and its snapshots hold ciphertext, not base64. Check with
`docker exec k3d-lifeboat-server-0 k3s secrets-encrypt status` (laptop) or
`sudo k3s secrets-encrypt status` (EC2). A cluster created before this change is not encrypted:
recreate it (`make cluster-down cluster`), or follow
[k3s secrets-encrypt](https://docs.k3s.io/cli/secrets-encrypt) to turn it on in place.

## When something goes wrong

| You see | Do |
|---|---|
| `ImagePullBackOff` / `ErrImagePull` | the image is not on that node: `make import restart` |
| pods `Pending`, "Insufficient cpu" | expected during the surge; that is what Lifeboat triages |
| `secret "lifeboat-s3" not found` | `make secrets` |
| Lifeboat restarting | `kubectl -n lifeboat logs deploy/lifeboat --previous` |
| `make itest` fails | it prints which check failed; the Lease/TriagePlan code is in `lifeboat/lease.py`, `k8s_plan_store.py`, `executor.py` |
| AWS "credentials expired" | copy fresh ones from AWS Details |

## What CI checks on every push

| Job | What |
|---|---|
| `test` | unit tests |
| `manifests` | yamllint (duplicate keys), kubeconform strict schema check, shellcheck |
| `cluster` | starts a real k3s control plane, `make deploy workloads`, then `make itest` as the lifeboat ServiceAccount |
| `images` | builds the 3 images and smoke-tests them (imports, webapp `/`, `/metrics`) |

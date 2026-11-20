# Lifeboat: testing instructions (CSC3104 Group 21)

<!-- Source of GROUP21_INSTRUCTIONS.pdf: pandoc docs/TESTING.md -o x.docx, then export the .docx to PDF. -->

Follow the steps in order; each says what you should see.
Steps 1-2 need only Python (2 minutes). Steps 3-8 run the whole system on a laptop cluster
(about 30 minutes, most of it waiting).

Repository: https://github.com/aarrcane/lifeboat, submitted tag **`v1.0-submission`**.

## 0. What you need

| Tool | Check |
| --- | --- |
| Docker (Docker Desktop on Windows/macOS), with at least **4 CPUs and 8 GB RAM** | `docker info` |
| k3d (runs k3s v1.31.4 in Docker) | `k3d version` |
| kubectl | `kubectl version --client` |
| helm 3 | `helm version` |
| make, git, Python 3.12 | `python3 --version` |

**Windows:** run every command inside a WSL2 Ubuntu terminal (the scripts are bash), with Docker
Desktop's WSL integration turned on. Install commands for each tool are in `docs/CLUSTER.md`, section A.1.

## 1. Get the code and the Python packages

```bash
git clone https://github.com/aarrcane/lifeboat && cd lifeboat
git checkout v1.0-submission
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
```

## 2. Unit tests (no cluster)

```bash
make test
```
Expected: `130 passed` (all green). These cover the 2-of-3 voter, signed readings and the
ingest checks, the planner, the executor's crash and takeover logic (Section 2.4 of the report),
the forecast, checkpoints, the feed parsers and the experiment maths.

## 3. Start the cluster and everything on it

```bash
make up
```
This creates a 3-server k3s cluster with embedded etcd in Docker (each server offers 2 CPUs),
builds the three images, creates the secrets (a local `.env` with random keys), MinIO for
checkpoints, the Lifeboat controller (2 replicas), Prometheus and Grafana, and the demo
workloads C1-C4. It takes about 10 minutes the first time. Then:

```bash
make status
```
Expected: 3 nodes `Ready`; in namespace `lifeboat` two `lifeboat-...` pods `Running 1/1` and
`minio`; in `lifeboat-demo` deployments `c1-emergency 2/2`, `c2-portal 2/2`, `placeholder 2/2`
and the jobs `c3-analytics` and `c4-backup` running.

## 4. Integration test against the cluster

```bash
make itest
```
It scales Lifeboat to 0, then drives two controller instances as the `lifeboat`
ServiceAccount (so the permissions are tested too), and restores Lifeboat afterwards.
Expected: every line `PASS` and `RESULT: ALL PASSED` (about 30 s). It checks: only the Lease
holder acts; the plan is written before acting; the standby takes over with takeover number 2
and the old copy can no longer act; everything is restored when the crisis ends; a human
override sent to the standby is carried out by the leader.

## 5. The flood demo

Open a second terminal (same folder) to watch:
```bash
kubectl -n lifeboat get triageplans -w
```
In the first terminal:
```bash
kubectl -n lifeboat set env deploy/lifeboat LIFEBOAT_CALM_MINUTES=1   # shorter step-down for the demo
make demo
```
`make demo` starts a 20x traffic surge (Locust) and replays the experiment's warnings. Expected,
counting from when the replay starts:

| Time | What you should see |
| --- | --- |
| ~75 s | NEA reports heavy rain: plan level **1**, `c4-backup` suspended, C1 grows to 3 pods |
| ~90 s | PUB and NEA agree: level **2**, placeholders scaled to 0, C1 grows ahead of the surge (7 pods on AWS) |
| 150 s | the surge starts; a few seconds later level **3** (C1 measurably slow): `c3-analytics` suspended, C2's extras switched off, C1 keeps growing |
| 330 s | the surge and the warnings end |
| then | one level down per minute; every workload back to its original settings, jobs resume from their checkpoint |

Check the batch jobs resumed where they stopped:
```bash
kubectl -n lifeboat-demo logs job/c3-analytics | grep -E "starting at|checkpoint"
```

## 6. Crash the active controller mid-triage

Run `make reset`, then `make demo` again. While the plan is at level 2 or 3:
```bash
HOLDER=$(kubectl -n lifeboat get lease lifeboat-executor -o jsonpath='{.spec.holderIdentity}')
kubectl -n lifeboat delete pod "$HOLDER" --grace-period=0 --force
kubectl -n lifeboat get lease lifeboat-executor -o custom-columns=HOLDER:.spec.holderIdentity,TRANSITIONS:.spec.leaseTransitions -w
```
Expected within about 20 s: the holder changes to the other pod, `TRANSITIONS` (the takeover
number) goes up by 1, and the TriagePlan keeps its level and steps. The measured version of
this test is `python scripts/e5_crash_test.py --runs 3`.

## 7. Optional checks

- **Forged reading is refused:**
  ```bash
  kubectl -n lifeboat port-forward deploy/lifeboat 8081:8081 &
  curl -s -X POST localhost:8081/readings -d '{"source":"S1","level":2,"ts":1,"expires_at":9999999999,"sig":"forged"}'
  ```
  Expected: `{"error": "bad_signature"}`.
- **Human override:** see "Human override" in `README.md` (lock at L2, then clear).
- **Live data.gov.sg feeds:** `make feeds`, wait 5 minutes, then
  `kubectl -n lifeboat logs -l app=lifeboat-feed --tail=5`. Expected: `"sent": [202, 202]`
  (each reading reached both replicas). Turn them off with `make feeds-off`.
- **Dashboard:** `kubectl -n monitoring port-forward svc/kps-grafana 3000:80`, open
  http://localhost:3000, dashboard "Lifeboat: crisis triage" (password command in `docs/CLUSTER.md`).
- **A server dies, the cluster lives:** `docker stop k3d-lifeboat-server-1`, `kubectl get nodes`
  still answers and `kubectl -n lifeboat get triageplans` still works; `docker start k3d-lifeboat-server-1`.

## 8. Clean up

```bash
make reset           # back to a clean L0 between runs
make cluster-down    # delete the whole laptop cluster
```

## If something goes wrong

| You see | Do |
| --- | --- |
| `ImagePullBackOff` | `make import restart` |
| pods `Pending`, "Insufficient cpu" during the demo | expected: that is what Lifeboat triages |
| `make itest` fails | it names the failing check; `kubectl -n lifeboat logs deploy/lifeboat` |
| Docker runs out of memory | give Docker 8 GB, or skip `make monitoring` (`make cluster import deploy workloads wait`) |

The experiments in the report (E1-E5) ran on AWS; `docs/CLUSTER.md` section B rebuilds that
cluster and `make experiment RUNS=3` repeats them (about 2.5 hours).

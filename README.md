# Lifeboat: Crisis-Aware Capacity Triage on Kubernetes
CSC3104 Group 21 - see `docs/WORK_SPLIT.md` for who builds what.

## Layout
| Path | What | Proposal ref |
|---|---|---|
| `lifeboat/voter.py` | 2-of-3 vote, expiry/signature check, calm period | Alg. 1, Table 3 |
| `lifeboat/planner.py` | greedy "cheapest work to lose first" plan | Alg. 2 |
| `lifeboat/executor.py` | idempotent steps, plan-first, takeover fencing, slow revert | Alg. 3, Fig. 2 |
| `lifeboat/lease.py`, `k8s_plan_store.py` | Lease leader election; TriagePlan CRD with CAS writes | Table 1 #2, #4 |
| `lifeboat/k8s_actions.py`, `checkpoint.py` | target-state actions; checkpoint-before-pause contract (S3) | Table 1 #3 |
| `lifeboat/forecast.py`, `monitor.py` | Holt forecast (S3), Prometheus + C1-struggling (L3) | Sec. 4 |
| `lifeboat/controller.py`, `operator.py` | sense-decide-act loop, Kopf entrypoint | Fig. 1 |
| `feeds/` | PUB (S1) / NEA (S2) serverless handlers, replayer | Sec. 5 |
| `workloads/` | C1/C2 web app, C3/C4 checkpointing worker | Sec. 6 |
| `deploy/` | CRD, RBAC, 2-replica Lifeboat, demo tiers, PodMonitors | |
| `loadtest/` | Locust 20x surge | E1/E2 |
| `tests/` | unit tests incl. crash-takeover scenario (Fig. 2) | E4/E5 |

## Quick start (laptop: Docker + k3d + kubectl + helm)
Full guide, AWS run and troubleshooting: **[docs/CLUSTER.md](docs/CLUSTER.md)**. `make help` lists every target.
```bash
pip install -r requirements-dev.txt && make test        # logic tests, no cluster needed
make cluster import                                      # 3-server k3d cluster, images on every node
make deploy monitoring workloads wait                    # secrets, MinIO, controller x2, Prometheus, demo tiers
make itest                                               # real-cluster check: Lease, TriagePlan, takeover, revert
make demo                                                # 20x surge + flood replay, from inside the cluster
kubectl -n lifeboat get triageplans -w                   # watch the plan
make reset                                               # clean L0 before the next run
```
In the cluster, checkpoints go to S3: MinIO on laptops (`deploy/05-checkpoint-store.yaml`), a real bucket on AWS (`S3_MODE=aws` in `.env`). Secrets come from your local `.env` (`make secrets`), never from git. The `file` backend is only for unit tests and running the worker on your laptop, since pods don't share `/tmp`.

## Baselines
- **B0** pod priority only: apply `deploy/01-priorityclasses.yaml` + `04-demo-workloads.yaml`, do not deploy Lifeboat.
- **B1** spare room: B0 + the `placeholder` Deployment (already in `04`).
- **B2** reactive Lifeboat: `LIFEBOAT_EARLY_WARNING=false` (ignores S1-S3, only the measured L3 path fires).

## Status / known gaps (be honest in the report)
- Unit-tested: voter, planner, executor crash/takeover, forecast, checkpoint, feed parsers.
- Tested against a real k3s API server (`make itest`, also in CI) as the lifeboat ServiceAccount: `lease.py`, `k8s_plan_store.py`, `k8s_actions.py`, `discovery.py`, `controller.py`, RBAC and all manifests. Covers L2 triage, standby takeover with fencing, and revert to original settings.
- **Not yet run with real pods** (needs images on nodes): checkpoint save/resume through MinIO, Prometheus queries, the replay and Locust Jobs. First `make demo` on k3d will tell.
- `PUB_URL` / `NEA_URL` and parsers must be checked against the live data.gov.sg responses.
- Voter calm-period state is per replica (not shared); a freshly promoted standby may lag by one calm period when stepping down.
- Thresholds N, X, Y, NEA mm cut-offs, `cost_millis_per_rps` need tuning (`lifeboat/config.py`).
- Everyone deploying to the same cluster needs the same `.env` (shared privately, see docs/CLUSTER.md).

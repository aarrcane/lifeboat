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

## Quick start (laptop, k3s)
```bash
pip install -r requirements-dev.txt && make test        # logic tests, no cluster needed
make k3s-import deploy monitoring workloads              # images -> k3s, controller, Prometheus, demo tiers
python scripts/make_scenario.py
python -m feeds.replay data/scenario_flash_flood.jsonl --speed 10   # run inside the cluster network
kubectl -n lifeboat get triageplans current -o yaml -w   # watch the plan
```
Set `LIFEBOAT_CHECKPOINT_BACKEND=file` (default) for quick tests, `s3` + `LIFEBOAT_S3_ENDPOINT` for MinIO/AWS.

## Baselines
- **B0** pod priority only: apply `deploy/01-priorityclasses.yaml` + `04-demo-workloads.yaml`, do not deploy Lifeboat.
- **B1** spare room: B0 + the `placeholder` Deployment (already in `04`).
- **B2** reactive Lifeboat: `LIFEBOAT_EARLY_WARNING=false` (ignores S1-S3, only the measured L3 path fires).

## Status / known gaps (be honest in the report)
- Unit-tested: voter, planner, executor crash/takeover, forecast, checkpoint, feed parsers.
- **Not yet run on a real cluster**: `lease.py`, `k8s_plan_store.py`, `k8s_actions.py`, `discovery.py`, `controller.py`, manifests. Expect small fixes.
- `PUB_URL` / `NEA_URL` and parsers must be checked against the live data.gov.sg responses.
- Voter calm-period state is per replica (not shared); a freshly promoted standby may lag by one calm period when stepping down.
- Thresholds N, X, Y, NEA mm cut-offs, `cost_millis_per_rps` need tuning (`lifeboat/config.py`).
- Change `LIFEBOAT_HMAC_SECRET` in `deploy/03-lifeboat.yaml` before sharing the repo.

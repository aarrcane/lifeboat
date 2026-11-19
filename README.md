# Lifeboat: Crisis-Aware Capacity Triage on Kubernetes
CSC3104 Group 21 - see `docs/WORK_SPLIT.md` for who builds what.

## Layout
| Path | What | Proposal ref |
|---|---|---|
| `lifeboat/voter.py` | 2-of-3 vote, expiry/signature check, calm period | Alg. 1, Table 3 |
| `lifeboat/planner.py` | greedy "cheapest work to lose first" plan | Alg. 2 |
| `lifeboat/executor.py` | idempotent steps, plan-first, takeover fencing, slow revert | Alg. 3, Fig. 2 |
| `lifeboat/lease.py`, `k8s_plan_store.py` | Lease leader election; TriagePlan CRD with CAS writes | Table 1 #2, #4 |
| `lifeboat/override_store.py` | human override shared by both replicas (ConfigMap) | Sec. 4 |
| `lifeboat/k8s_actions.py`, `checkpoint.py` | target-state actions; checkpoint-before-pause contract (S3) | Table 1 #3 |
| `lifeboat/forecast.py`, `monitor.py` | Holt forecast (S3), Prometheus + C1-struggling (L3) | Sec. 4 |
| `lifeboat/forecast_eval.py`, `scripts/demo_p6.py` | forecast benchmark + alpha/beta/horizon sweep | E4 (S3) |
| `scripts/make_dashboard.py`, `deploy/monitoring/grafana-dashboard.yaml` | Grafana dashboard (18 panels in five sections) | E1-E5 |
| `scripts/p6_live_check.py` | real Prometheus + webapp + Lifeboat loop, no cluster | E1-E5 |
| `deploy/monitoring/alert-rules.yaml` | 8 Prometheus alerts, drawn on the dashboard | video |
| `scripts/plot_results.py`, `export_e4.py`, `e5_crash_test.py` | results CSVs -> figures; E4 export; E5 kill test on a cluster | E1-E5 |
| `docs/RESULTS_FORMAT.md`, `docs/report/` | CSV contracts; report draft and video plan | report |
| `lifeboat/controller.py`, `operator.py` | sense-decide-act loop, Kopf entrypoint | Fig. 1 |
| `feeds/`, `deploy/feeds/` | PUB (S1) / NEA (S2) feed functions, run as CronJobs (`make feeds`; OpenFaaS packaging in `feeds/stack.yml`), replayer | Sec. 4.1, 5 |
| `workloads/` | C1/C2 web app, C3/C4 checkpointing worker | Sec. 6 |
| `deploy/` | CRD, RBAC, 2-replica Lifeboat, demo tiers, PodMonitors | |
| `loadtest/` | Locust 20x surge | E1/E2 |
| `tests/` | unit tests incl. crash-takeover scenario (Fig. 2) | E4/E5 |

## Quick start (laptop: Docker + k3d + kubectl + helm)
Full guide, AWS run and troubleshooting: **[docs/CLUSTER.md](docs/CLUSTER.md)**. `make help` lists every target.
```bash
pip install -r requirements-dev.txt && make test        # logic tests, no cluster needed
make up                                                  # = make cluster import deploy monitoring workloads wait:
                                                         #   3-server k3d cluster, images, secrets, MinIO,
                                                         #   controller x2, Prometheus, demo tiers
make itest                                               # real-cluster check: Lease, TriagePlan, takeover, revert
make demo                                                # 20x surge + flood warning 60 s ahead, speed 1 (~8 min)
make experiment RUNS=1                                   # measure E1/E2/E3 for B0, B1, B2, lifeboat
kubectl -n lifeboat get triageplans -w                   # watch the plan
make reset                                               # clean L0 before the next run
```
In the cluster, checkpoints go to S3: MinIO on laptops (`deploy/05-checkpoint-store.yaml`), a real bucket on AWS (`S3_MODE=aws` in `.env`). Secrets come from your local `.env` (`make secrets`), never from git. The `file` backend is only for unit tests and running the worker on your laptop, since pods don't share `/tmp`.

## Baselines
All four setups run on the same workloads, load and replayed warnings, with the same cool-down
(HPA scale-down window 60 s, `LIFEBOAT_CALM_MINUTES=1`). `scripts/run_experiment.py` sets them up.
- **B0** HPA + pod priority: `deploy/01-priorityclasses.yaml`, `04-demo-workloads.yaml` and the C1 autoscaler `deploy/baselines/hpa-c1.yaml` (CPU 70 %, 2-20 pods); Lifeboat scaled to 0, placeholders 0.
- **B1** HPA + pod priority + spare room: B0 + 2 `placeholder` pods (already in `04`).
- **B2** Lifeboat reacting only to measured distress: `LIFEBOAT_EARLY_WARNING=false` (ignores S1-S3, only the measured L3 path fires), no HPA.
- **lifeboat** full Lifeboat, no HPA. The HPA must never run next to Lifeboat: two controllers writing C1's replicas fight.

## Human override
An operator can lock the level (for a drill, or to stop triage) through either Lifeboat pod. It is
stored in the ConfigMap `lifeboat-override`, so every replica applies it and it survives a takeover.
Every override is logged and counted (`lifeboat_overrides_total`).
```bash
TOKEN=$(kubectl -n lifeboat get secret lifeboat-secrets -o jsonpath='{.data.LIFEBOAT_HMAC_SECRET}' | base64 -d)
kubectl -n lifeboat port-forward deploy/lifeboat 8081:8081 &
curl -s -X POST localhost:8081/override -H "X-Lifeboat-Token: $TOKEN" -d '{"level": 2, "who": "alice"}'     # lock at L2
curl -s -X POST localhost:8081/override -H "X-Lifeboat-Token: $TOKEN" -d '{"level": null, "who": "alice"}'  # back to the vote
```
`level` must be `null` or 0-3 (anything else: 400). `make reset` removes the override.

## Status (as of the final AWS runs, 14-15 Nov)
- **Measured on AWS** (3 x t3.large, k3s with embedded etcd): E1-E3 for B0, B1, B2 and Lifeboat, 3 runs each (`results/e1_e2_summary.csv`, `results/runs/`); E5 kill test (`results/e5_crash.csv`); evidence in `results/aws_evidence/`. E4 is an offline replay through the real voter (`results/e4_alerts.csv`).
- **Unit tests** (`make test`): voter, ingest validation, planner, executor crash/takeover and one-undo-per-tick, forecast, checkpoint, discovery, feed parsers, controller ticks (takeover keeps the crisis, outage silences the forecast vote, override shared across replicas).
- **Integration test** (`make itest`, also in CI) against a real k3s API server as the lifeboat ServiceAccount, so the namespaced RBAC is checked too: L2 triage, standby takeover with fencing, revert to original settings, and a human override sent to the standby that the leader carries out.
- **P6 monitoring and forecast**: every dashboard query, the PromQL in `config.py` and the alert rules checked against a real Prometheus scraping the real webapp (`scripts/p6_live_check.py`, no cluster). Forecast alpha tuned to 0.3 by `scripts/demo_p6.py` on synthetic traces; the sweep was re-run after the forecast clamp fix (f8007ba) with the same result: 81 s lead on a gradual surge, 0 false L2. Not yet confirmed on the AWS cluster: PodMonitor scraping and the Grafana sidecar loading the dashboard.
- **Shared between replicas** through etcd: the TriagePlan (level, steps, takeover number) and the human override. Still per replica: the calm-period timer (a new leader steps down one calm period after it took over) and the reading book (feeds send every reading to both pods).
- **Known limits** (also in the report): C1 is sized from the traffic it *serves*, so a sudden surge is followed step by step; the planner checks cluster-wide free CPU, not per-node fit; Lease expiry assumes NTP-synchronised clocks; the experiments shorten the calm period to 1 minute; one signing key for all feeds and the override.
- Everyone deploying to the same cluster needs the same `.env` (shared privately, see docs/CLUSTER.md).

# Results format (E1-E5)

Every experiment ends as one CSV in `results/`. `python scripts/plot_results.py` turns each CSV
into a figure in `results/figures/`, and says which CSVs are still missing. Run **3 times per
setup** (proposal Section 6) and put every run in the same CSV with a `run` column.

| Exp | CSV | Produced by | Status |
| --- | --- | --- | --- |
| E1/E2 | `e1_e2_summary.csv`, `runs/<setup>-<run>/timeline.csv` | `scripts/run_experiment.py` on the cluster | needs cluster runs. The old model numbers are in `model/e1_e2_model.csv` (`scripts/run_p5_baseline.py --dry-run`): a model prediction, never a result |
| E3 | `e3_work_saved.csv` | `scripts/run_experiment.py` from the worker events (`lifeboat/e3_collect.py`) | needs cluster runs |
| E4 | `e4_alerts.csv` | `scripts/export_e4.py` (P2's evaluation) | done (offline replay through the real voter) |
| E5 | `e5_crash.csv` | `scripts/e5_crash_test.py` on the cluster (P3 + P6) | **missing**: needs a cluster run |

## E1/E2 `e1_e2_summary.csv` (`scripts/run_experiment.py`)

One row per setup and run. Every setup sees the same load (`loadtest/locustfile.py`, 10 users,
200 users from t = 150 s to 330 s) and, for B2 and lifeboat, the same replayed warnings
(`data/scenario_experiment.jsonl`: L2 from t = 90 s, 60 s before the surge, until 330 s).

| Column | Meaning |
| --- | --- |
| `setup`, `run` | `B0` (HPA + pod priority), `B1` (B0 + spare room), `B2` (Lifeboat, warnings off), `lifeboat`; 1 to 3 |
| `e1_surge_to_capacity_s` | seconds from t = 150 until C1 has 10 ready pods (10 = 120 req/s x 20m / 250m). Negative if ready before the surge. Empty if never. Comparable across all setups. |
| `alert_to_ready_s` | B2 and lifeboat only: seconds from the first level change to all planned C1 pods ready (the proposal's "first alert to C1 healthy, target under 30 s") |
| `c1_ready_at_surge` | ready C1 pods at t = 150 |
| `c1_served_pct`, `p50_ms`, `p95_ms` | from Locust (`RESULT` lines), surge window only, C1. Served = came back as a 200 within 0.5 s |
| `c2_served_pct` | same for C2 (what L2 costs C2 and what L3 feature shedding gives back) |

`runs/<setup>-<run>/timeline.csv` (one row per second: `t,c1_ready,c1_spec,level,plan_steps,
c3_suspend,c3_active,c4_suspend,c4_active,placeholder_ready`) feeds `figures/e1_timeline.png`;
`locust.log` and `e3.log` are kept next to it as evidence.

```bash
python3 scripts/run_experiment.py --setup all --runs 1   # rehearsal (k3d)
python3 scripts/run_experiment.py --setup all --runs 3   # the matrix (AWS, on server-1, in tmux)
python scripts/plot_results.py
```

## E3 `e3_work_saved.csv` (P3)

| Column | Meaning |
| --- | --- |
| `setup` | `B0`, `B1`, `B2` or `lifeboat` |
| `run` | 1, 2, 3 |
| `workload` | `c3-analytics` or `c4-backup` |
| `progress_lost_s` | seconds of work redone after resuming (work since the last checkpoint that was used) |
| `restore_s` | seconds from crisis end until the workload is running again at its original settings |

How it is measured: each worker writes a lifecycle event through the checkpoint store
(`events/<name>/<ts>-<kind>.json`, `{"name","kind","unit","ts"}`) when it starts (after loading
its checkpoint), first thing on SIGTERM (`term`) and when it finishes (`done`). Pod logs vanish
when a Job is suspended, the store does not. After a run, `deploy/jobs/e3-collect.yaml` prints
them as `E3EVENT {...}` lines and the runner computes:

- `progress_lost_s` = for each `term` followed by the next `start` of the same workload,
  `max(0, term.unit - start.unit) x UNIT_SECONDS` (0.05 s), summed.
- `restore_s` = time of the last `start` after the crisis end (t = 330 s) minus t = 330; 0 if the
  workload was never interrupted; empty if it was not back within the wait.

The worker saves on SIGTERM by default. `SAVE_ON_TERM=false` models an app that ignores SIGTERM,
the case Lifeboat's "checkpoint before pause" is for. On SIGTERM with work left it exits 143, so
a preempted or deleted pod never marks the Job Complete.

Example:
```csv
setup,run,workload,progress_lost_s,restore_s
B0,1,c3-analytics,184,95
lifeboat,1,c3-analytics,12,61
```

## E4 `e4_alerts.csv` (generated)

`scenario,true_crisis,voter,peak_level,false_l2_alarms,false_l2_s,missed,flips`, one row per
scenario × voter (`naive` = any one source, `lifeboat` = 2 of 3). Regenerate with
`python scripts/export_e4.py`.

## E5 `e5_crash.csv` (`scripts/e5_crash_test.py`)

| Column | Meaning |
| --- | --- |
| `test` | `kill-active`, `kill-active-x3` (killed 3 times back to back), ... |
| `run` | 1, 2, 3 |
| `takeover_s` | kill → standby holds the Lease and has stamped a higher takeover number |
| `steps_total` | steps in the TriagePlan |
| `steps_left_half_done` | steps still not done after the settle time (target 0) |
| `steps_mismatched` | steps marked done whose workload is not in the target state (target 0) |
| `takeover_increment` | new takeover number − old (fencing, must be ≥ 1; summed over back-to-back kills) |
| `level_before`, `level_after` | TriagePlan level before the (first) kill and after the settle time |
| `steps_before`, `steps_after` | steps in the TriagePlan before and after |
| `plan_kept` | 1 if, watched every 0.5 s from the kill to the end of the settle, the level never dropped and no step disappeared (target 1). Without it the test would pass even if the new executor undid the triage. Run it while the scenario is still at L2. |
| `lifeboat_cpu_m`, `lifeboat_mem_mi` | Lifeboat's own cost per replica from `kubectl top` |

Run it while a crisis replay is going (Lifeboat at L2, plan written):

```bash
make demo                                          # P4: deploy + replay + load
python scripts/e5_crash_test.py --runs 3
python scripts/e5_crash_test.py --runs 3 --repeat 3
python scripts/plot_results.py
```

Steps set a target state, so redoing one is harmless. The proposal's "nothing done twice" is
measured as "nothing left half done and nothing drifted from the plan".

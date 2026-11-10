# Results format (E1-E5)

Every experiment ends as one CSV in `results/`. `python scripts/plot_results.py` turns each CSV
into a figure in `results/figures/`, and says which CSVs are still missing. Run **3 times per
setup** (proposal Section 6) and put every run in the same CSV with a `run` column.

| Exp | CSV | Produced by | Status |
| --- | --- | --- | --- |
| E1/E2 | `e1_e2_summary.csv` | P5 (`scripts/run_p5_baseline.py`) | baseline model numbers; replace with cluster runs |
| E3 | `e3_work_saved.csv` | P3 | **missing** |
| E4 | `e4_alerts.csv` | `scripts/export_e4.py` (P2's evaluation) | done (offline replay through the real voter) |
| E5 | `e5_crash.csv` | `scripts/e5_crash_test.py` on the cluster (P3 + P6) | **missing**: needs a cluster run |

## E1/E2 `e1_e2_summary.csv` (P5)

`setup,name,e1_time_to_capacity_s,c1_served_pct,p50_ms,p95_ms` with `setup` in `B0, B1, B2, lifeboat`.

## E3 `e3_work_saved.csv` (P3)

| Column | Meaning |
| --- | --- |
| `setup` | `B0`, `B1`, `B2` or `lifeboat` |
| `run` | 1, 2, 3 |
| `workload` | `c3-analytics` or `c4-backup` |
| `progress_lost_s` | seconds of work redone after resuming (work since the last checkpoint that was used) |
| `restore_s` | seconds from crisis end until the workload is running again at its original settings |

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
| `takeover_increment` | new takeover number − old (fencing, must be ≥ 1) |
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

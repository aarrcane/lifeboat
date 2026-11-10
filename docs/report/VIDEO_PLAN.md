# Demo video plan (P6)

Story from WORK_SPLIT R6.7: **normal → warning → L1 → L2 → kill executor → recovery → restore.**
Target length about 5 minutes. Record on the AWS cluster if it's up, otherwise on k3d.

## Before recording

```bash
make deploy monitoring workloads wait      # Lifeboat x2, Prometheus/Grafana, dashboard, alert rules, demo tiers
make reset                                 # clean L0
kubectl -n monitoring port-forward svc/kps-grafana 3000:80     # Grafana at http://localhost:3000
```

- Grafana: open **Lifeboat: crisis triage**, set the time range to *Last 15 minutes*, refresh 5 s.
  The default login is the `kps-grafana` Secret (`kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d`).
- Screen layout: Grafana on the left (about 2/3), a terminal on the right with:
  - `kubectl -n lifeboat get triageplans -w`
  - `kubectl -n lifeboat get lease lifeboat-executor -w -o custom-columns=HOLDER:.spec.holderIdentity`
  - `kubectl -n lifeboat-demo get jobs,deploy -w`

## Shot list

| # | Time | What happens | Command | What to show on screen | Narration (one line) |
| --- | --- | --- | --- | --- | --- |
| 1 | 0:00 | Title + problem | none | proposal Figure 1 | "Lifeboat makes room for emergency services before a flood surge, not after." |
| 2 | 0:30 | Normal | none | Alert level L0, RPS flat, C1 p95 well under 0.5 s, C3/C4 running | "Normal day: everything runs, including analytics and backups." |
| 3 | 1:00 | Flood replay starts | `make demo` | S2 NEA vote goes to L1, **Alert level L1**, annotation "LifeboatLevelChanged" | "NEA reports heavy rain. One source is enough for L1: the backup pauses and one extra C1 pod starts." |
| 4 | 1:40 | Second source agrees | (replay continues) | S1 PUB joins, **L2**, plan steps appear, C3 checkpoints then pauses, placeholder pods go | "PUB confirms. Two of three agree: L2. Analytics saves a checkpoint, then pauses; C1 is sized from the forecast." |
| 5 | 2:20 | Surge arrives | (Locust in `make demo`) | RPS climbs, forecast ratio crosses the Y line, S3 votes too; C1 p95 stays under target | "The traffic arrives after the room was made, and C1 stays fast." |
| 6 | 2:50 | Kill the active executor | `kubectl -n lifeboat delete pod <holder> --grace-period=0 --force` | Lease holder changes, takeover number +1, **no LifeboatNoActiveExecutor alert**, plan steps still complete | "We crash the active executor mid-triage. The standby takes the Lease with a higher takeover number and finishes the plan." |
| 7 | 3:40 | Recovery | (replay ends) | votes drop, level steps down one per calm period, C3/C4 resume from checkpoint | "When the warnings stop, Lifeboat waits for a calm period and steps down slowly." |
| 8 | 4:20 | Restore + results | `python scripts/plot_results.py` | E1–E5 figures | "Everything is back at its original settings. Here's how it compares with B0 to B2." |

## Tips

- Record each shot separately and cut; the replay and calm periods are long in real time.
  For the video, `LIFEBOAT_CALM_MINUTES=1` shortens step-down (say so in the narration).
- Zoom the Grafana panels you talk about; viewers can't read 11 panels at once.
- Keep the terminal font large (at least 16 pt).
- Shot 6 is the most important for marks (Challenge 4). Rehearse it once before recording.
- Alert annotations come from `deploy/monitoring/alert-rules.yaml`; if none appear, check
  `kubectl -n monitoring get prometheusrules lifeboat-alerts`.

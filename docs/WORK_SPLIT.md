# Work split for 6 members

Deadlines: **29 Oct** progress demo, **15 Nov** final (video + report + AWS run).
Rule of thumb: each role owns files (below) -> fewer merge conflicts. Work on feature branches, PR into `main`, one reviewer per PR (reviewer pairs listed). Run `make test` before every PR.

| # | Role | Owns | Experiments | Review buddy |
|---|------|------|-------------|--------------|
| R1 | Executor & HA lead (tech lead) | `executor.py`, `lease.py`, `plan_store.py`, `k8s_plan_store.py`, `k8s_actions.py`, `tests/test_executor.py` | E5 | R4 |
| R2 | Signals & Voter | `voter.py`, `signing.py`, `ingest.py`, `feeds/`, `scripts/make_scenario.py` | E4 | R6 |
| R3 | Planner & Checkpointing | `planner.py`, `checkpoint.py`, `discovery.py`, `workloads/worker/` | E3 | R1 |
| R4 | Cluster & DevOps | `deploy/`, Dockerfiles, `Makefile`, CI, k3s HA, AWS EC2, S3/MinIO, repo admin | infra for all | R1 |
| R5 | Workloads, Load & Baselines | `workloads/webapp/`, `loadtest/`, B0/B1/B2 configs | E1, E2 | R3 |
| R6 | Observability, Forecast & Report | `forecast.py`, `monitor.py`, `metrics.py`, Grafana dashboards, report + video | E1-E5 plots | R2 |

## Shared first step (everyone, day 1-2, ~1 h)
1. R4 creates the GitHub repo, pushes this code, enables branch protection on `main` (1 review + CI green), adds everyone.
2. Everyone: clone, `pip install -r requirements-dev.txt`, `make test` (should show all green), read `README.md` and proposal Sections 4 and Appendix D.
3. Create GitHub Issues from the checklists below; label by role and by milestone (`progress-29oct`, `final-15nov`).

---
## R1 - Executor & HA lead
**Goal:** a crash at any point never leaves triage half done; the standby finishes it (challenges 2 and 4).
By 29 Oct
1. Run `lifeboat/lease.py` against k3s (use R4's cluster): start two controller pods, confirm only one has `lifeboat_is_active_executor=1`; `kubectl delete pod` the active one, confirm the other takes over and `leaseTransitions` increments.
2. Exercise `K8sPlanStore`: `kubectl get triageplans current -o yaml`; verify `resourceVersion` conflicts are retried and that a write with a lower takeover raises `Fenced`.
3. Verify `K8sActions` per action type (suspend Job, scale Deployment, patch ConfigMap) are idempotent: apply twice, state identical; revert restores original.
4. Run the **executor kill test** (progress milestone): trigger L2, `kubectl delete pod` the active executor mid-plan, show the plan finishes with takeover number 2 and no step is half done. Capture logs/screens for the report.
By 15 Nov
5. Close the "late write by old executor" gap noted in Algorithm 3: before each `apply`, `still_leader()` is checked; add a chaos test that pauses the old executor (SIGSTOP), lets the lease expire, resumes it, and shows its write is fenced.
6. Slow, ordered revert: confirm one step undone per tick and `is_reverted` checks original settings; add tuning knob for revert pace.
7. E5 with R6: kill active executor, kill executor pods repeatedly, cut a feed; record "nothing done twice or left half done"; measure Lifeboat CPU/memory (`kubectl top`).
8. Write the HA/fencing sections of the report (Table 1 rows 2 and 4, Figure 2).

## R2 - Signals & Voter
**Goal:** unreliable warnings never trigger big pauses (challenge 1).
By 29 Oct
1. Find the real PUB and NEA response shapes (open refs [2], [3]); set `PUB_URL`/`NEA_URL`; save sample payloads under `tests/fixtures/` and extend `tests/test_feeds.py` to parse them.
2. Tune NEA cut-offs (`NEA_L1_MM`, `NEA_L2_MM`) using a few recorded rainy days; justify in the report.
3. Run `ingest.py` locally (`curl -X POST /readings`) with good, forged and expired readings; check `lifeboat_rejected_readings_total`.
4. Hook feeds into the voter via a running controller and confirm: 1 source -> L1; 2 sources -> L2; forged/expired ignored.
By 15 Nov
5. Deploy the feeds as OpenFaaS functions on k3s (`feeds/stack.yml`) with cron triggers (2 min PUB, 5 min NEA); confirm each reading reaches **both** Lifeboat pods (headless Service).
6. Build 3 replay scenarios for E4 (noisy, conflicting, no-crisis) plus the main flash flood; extend `scripts/make_scenario.py`.
7. E4: count false alarms, missed crises, level flips; compare with a naive "any source -> L2" voter (write it as a flag for comparison).
8. Document the human-override endpoint (`POST /override` with `X-Lifeboat-Token`) and make sure every override is logged.

## R3 - Planner & Checkpointing
**Goal:** pause the cheapest work first and never lose it (challenge 3).
By 29 Oct
1. Read `planner.py`; run `tests/test_planner.py`; add 3+ edge-case tests (no C3/C4 present, shortfall bigger than all candidates, overdue backup).
2. Run `workloads/worker` locally with `LIFEBOAT_CHECKPOINT_BACKEND=file`: start, `kill -TERM`, restart; it must resume from the saved unit. Test the "save now" request path with `CheckpointStore.request()`.
3. Switch the worker to S3/MinIO (R4 provides endpoint + keys); confirm the manifest (`latest.json`) is written **after** the data object.
4. Verify `discovery.list_workloads` reads tiers, restart-seconds and overdue annotations from the demo Jobs (`deploy/04-demo-workloads.yaml`).
By 15 Nov
5. Validate that paused Jobs resume from checkpoint on un-suspend (Job pods are recreated); measure "progress lost" and "time until everything is back" for E3.
6. Tune scoring `freed / (work since checkpoint + restart time)`; run a comparison against random order and "largest first" to justify the greedy rule.
7. Check the stability rule: planner uses pre-triage inputs (`apply_originals`, `delta_millis`) - add a test that applying the plan then re-planning gives the same steps.
8. E3 data + report section for Algorithm 2 and Table 1 row 3.

## R4 - Cluster & DevOps
**Goal:** everyone can deploy in one command; final run on AWS.
By 29 Oct
1. Create the GitHub repo, push, set protections, CI (`.github/workflows/ci.yml`) green.
2. Stand up a 3-server k3s cluster with embedded etcd (`k3s server --cluster-init`, then `--server https://<ip>:6443` for the others); write the exact commands to `docs/CLUSTER.md`.
3. `make k3s-import deploy workloads`; fix any manifest/RBAC errors (first real run of `deploy/`).
4. Install monitoring (`make monitoring`), give R6 a Grafana URL.
5. Provide MinIO (laptop) with bucket `lifeboat-checkpoints`; put credentials in the Secret.
By 15 Nov
6. Terraform or documented AWS CLI steps for 3 EC2 instances (e.g. t3.large), security groups, S3 bucket + IAM user; push images to ECR or import to k3s.
7. Switch checkpoints to real S3; re-run smoke tests on EC2; take a snapshot/AMI so the final run is repeatable.
8. Prepare a `make demo` target (deploy + replay + load) and a teardown script to avoid AWS cost; set a budget alarm.

## R5 - Workloads, Load & Baselines
**Goal:** a fair, repeatable surge test on B0/B1/B2/Lifeboat (E1, E2).
By 29 Oct
1. Build/run `workloads/webapp` locally; tune `BURN_MS` so 1 pod ~ N req/s at 100% of its CPU request.
2. Deploy tiers; get B0 (pod priority only) and B1 (+ placeholders) running; document how to reset between runs.
3. Run `loadtest/locustfile.py` against B0; capture C1 request counts and p50/p95.
By 15 Nov
4. Make runs deterministic: same Job parallelism, same checkpointing workloads, same node sizes, same surge shape for every setup.
5. Run each setup >= 3 times: B0, B1, B2 (`LIFEBOAT_EARLY_WARNING=false`), full Lifeboat. Script with Locust CSV output into `results/`.
6. E1: time from first alert to C1 healthy (target < 30 s); E2: C1 served + typical/slow latency; hand CSVs to R6.
7. Calibrate with R6 the C1 response-time target and `cost_millis_per_rps`.

## R6 - Observability, Forecast & Report
**Goal:** evidence + story (dashboard, forecast, report, video).
By 29 Oct
1. Grafana dashboard: alert level, per-source level, C1 p95 and RPS, plan steps done/pending, active executor + takeover number, node CPU free.
2. Run `forecast.py` against recorded traffic from R5; choose alpha/beta/horizon; check S3 reaches L1/L2 with sensible X and Y.
3. Verify `monitor.py` queries return values (fix PromQL if labels differ); test behaviour when Prometheus is down (should not crash).
By 15 Nov
4. Add Prometheus alert/annotation lines on the dashboard for level changes (for the video).
5. Own the data pipeline: `results/` CSVs -> plotting script -> figures for E1-E5 (tables 2 format).
6. Own the report (Section structure from the proposal), merge each member's section, check references and that every claim has data; add "known gaps" honestly.
7. Record and edit the demo video: normal -> warning -> L1 -> L2 -> kill executor -> recovery -> restore.

---
## Timeline
| Date | Milestone |
|---|---|
| 15-17 Oct | Repo up, everyone runs tests; R4 starts k3s cluster; issues created |
| 18-24 Oct | First real cluster run; each role's "By 29 Oct" items 1-3 |
| 25-28 Oct | Integration day: end-to-end replay on k3s; executor kill test; B0/B1 numbers; dashboard |
| **29 Oct** | **Progress demo** |
| 30 Oct-5 Nov | Feeds live via OpenFaaS, restore checks, forecast tuning, AWS setup |
| 6-11 Nov | All five experiments, 3 runs each on AWS EC2 |
| 12-14 Nov | Report, video, polish, freeze `main` on 13 Nov |
| **15 Nov** | **Final submission** |

## Integration contract (so roles don't block each other)
- Reading JSON: `{source, level, ts, expires_at, sig}` signed with HMAC (`signing.py`).
- Tier label: `lifeboat.io/tier: C1|C2|C3|C4|placeholder`.
- Checkpoint contract: `checkpoint.py` docstring (data first, `latest.json` last; `requests/<name>.json` = save now).
- TriagePlan CR: `kubectl get triageplans -n lifeboat`.
- Metrics names: `lifeboat/metrics.py`.

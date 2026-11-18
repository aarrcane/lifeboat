from lifeboat.config import Config
from lifeboat.models import Level, Tier, Workload
from lifeboat.planner import make_plan

CFG = Config()


def wl():
    return [
        Workload("c1-api", "ns", Tier.C1, "Deployment", 250, 3),
        Workload("analytics", "ns", Tier.C3, "Job", 500, 2, last_checkpoint_ts=990),
        Workload("backup", "ns", Tier.C4, "Job", 500, 1, last_checkpoint_ts=100),
        Workload("placeholder", "ns", Tier.PLACEHOLDER, "Deployment", 500, 2),
    ]


def keys(steps):
    return [s.key for s in steps]


def test_l0_nothing():
    assert make_plan(Level.L0, wl(), 10, 0, CFG, now=1000) == []


def test_l1_pauses_c4_and_adds_c1_pod_only():
    s = make_plan(Level.L1, wl(), 10, 0, CFG, now=1000)
    assert keys(s) == ["suspend_job:ns/backup", "scale_deployment:ns/c1-api"]
    assert s[1].params["replicas"] == 4


def test_l1_overdue_backup_keeps_running():
    w = wl(); w[2].overdue = True
    assert "suspend_job:ns/backup" not in keys(make_plan(Level.L1, w, 10, 0, CFG, now=1000))


def test_l2_pauses_until_shortfall_covered_cheapest_loss_first():
    # needs 100 rps * 20 + 500 = 2500m, has 750m -> +1750m; no free room
    s = make_plan(Level.L2, wl(), 100, 0, CFG, now=1000)
    k = keys(s)
    assert k[0] == "suspend_job:ns/backup"
    assert "suspend_job:ns/analytics" in k
    assert "scale_deployment:ns/placeholder" in k
    assert next(x for x in s if x.name == "c1-api").params["replicas"] >= 10


def test_l2_no_extra_pauses_when_room_is_free():
    s = make_plan(Level.L2, wl(), 5, 10_000, CFG, now=1000)
    assert "suspend_job:ns/analytics" not in keys(s)


def test_l3_pauses_all_and_degrades_c2():
    s = make_plan(Level.L3, wl(), 5, 10_000, CFG, now=1000)
    assert {"suspend_job:ns/analytics", "suspend_job:ns/backup"} <= set(keys(s))
    assert any(x.action == "set_configmap" for x in s)


# ---- A7: C1 sizing rules (forecast clamp, L2 pre-size, ratchet)

from lifeboat import ingest  # noqa: E402
from lifeboat.forecast import TrafficForecaster  # noqa: E402
from lifeboat.models import Reading  # noqa: E402
from lifeboat.planner import c1_sizing_rps  # noqa: E402
from lifeboat.signing import sign  # noqa: E402
from lifeboat.voter import Voter  # noqa: E402


def demo_wl(c1_replicas=2):
    return [
        Workload("c1-emergency", "ns", Tier.C1, "Deployment", 250, c1_replicas),
        Workload("c2-portal", "ns", Tier.C2, "Deployment", 250, 2),
        Workload("c3-analytics", "ns", Tier.C3, "Job", 1000, 1, last_checkpoint_ts=990),
        Workload("c4-backup", "ns", Tier.C4, "Job", 500, 1, last_checkpoint_ts=970),
        Workload("placeholder", "ns", Tier.PLACEHOLDER, "Deployment", 500, 2),
    ]


def c1_target(steps, default=2):
    return next((s.params["replicas"] for s in steps if s.name == "c1-emergency"), default)


def test_forecast_overshoot_is_clamped_to_observed_traffic():
    # Holt predicts 339 req/s on a step surge whose true peak is 120 (forecast_overshoot.py)
    assert c1_sizing_rps(Level.L2, 339, 120, 6, CFG) == 150
    assert c1_sizing_rps(Level.L1, 30, 120, 6, CFG) == 120        # never below what we see
    assert c1_sizing_rps(Level.L1, 50, None, 6, CFG) == 50        # no observation: forecast
    s = make_plan(Level.L2, demo_wl(), c1_sizing_rps(Level.L2, 339, 120, 6, CFG), 0, CFG, now=1000)
    assert c1_target(s) == 14                                     # (150 x 20 + 500) / 250, not 20


def test_l1_with_high_observed_traffic_scales_c1():
    rps = c1_sizing_rps(Level.L1, 120, 120, 6, CFG)
    s = make_plan(Level.L1, demo_wl(), rps, 0, CFG, now=1000)
    assert c1_target(s) == 12                                     # (120 x 20 + 500) / 250
    assert "suspend_job:ns/c3-analytics" not in keys(s)           # L1 still pauses only C4


def test_l2_with_normal_traffic_presizes_for_10x_baseline():
    rps = c1_sizing_rps(Level.L2, 6, 6, 6, CFG)
    assert rps == 60
    assert c1_target(make_plan(Level.L2, demo_wl(), rps, 0, CFG, now=1000)) == 7


def test_c1_target_never_drops_while_a_plan_exists():
    s = make_plan(Level.L1, demo_wl(), 6, 0, CFG, now=1000, c1_floor={"c1-emergency": 14})
    assert c1_target(s) == 14
    assert make_plan(Level.L0, demo_wl(), 6, 0, CFG, now=1000, c1_floor={"c1-emergency": 14}) == []


def test_sizing_rules_on_the_experiment_timeline():
    """plan-evidence/sizing_rules_sim.py as a test: real voter, forecasters and planner on the
    A4 timeline (L2 warning at 90 s, surge 150-330 s at 200 users, 60 % of them on C1)."""
    cfg = Config(calm_minutes=1.0, forecast_warmup_samples=4)

    def users(t):
        return 10 if t < 150 else 200 if t < 330 else 10 if t < 360 else 0

    def rate1m(t):
        return sum(users(max(0, t - k)) for k in range(60)) / 60.0

    ttl = {"S1": 150.0, "S2": 330.0}
    events = [(0, "S1", 0), (0, "S2", 0), (75, "S2", 1)] + \
        [(t, s, 2) for t in range(90, 330, 60) for s in ("S1", "S2")] + [(330, "S1", 0), (330, "S2", 0)]
    book, voter = ingest.ReadingBook(), Voter(cfg.hmac_secret, 60, 600)
    traffic = TrafficForecaster(.3, .3, 12, 2, 4, 4)
    c1 = TrafficForecaster(.3, .3, 12, 2, 4, 4)
    floor, last, observed, seen = {}, -99, None, {}
    for t in range(0, 520, 5):
        for et, src, lvl in events:
            if t - 5 < et <= t or (t == 0 and et == 0):
                book.add(sign(Reading(src, lvl, et, et + ttl[src]), cfg.hmac_secret))
        if t - last >= 15:
            last = t
            traffic.observe(rate1m(t))
            observed = 0.6 * rate1m(t)
            c1.observe(observed)
            book.add(sign(Reading("S3", int(traffic.level()), t, t + 60), cfg.hmac_secret))
        level = voter.evaluate(book.snapshot(), t, False)
        rps = c1_sizing_rps(level, c1.predicted(), observed, c1.baseline, cfg)
        steps = make_plan(level, demo_wl(), rps, 0, cfg, now=1000, c1_floor=floor)
        target = c1_target(steps)
        floor = {"c1-emergency": target} if steps else {}
        seen[t] = (int(level), target)

    assert seen[90] == (2, 7)                     # L2 a minute before the surge: 7 pods already
    assert seen[195][1] == 12                     # 45 s into the surge
    assert max(tg for _, tg in seen.values()) == 14 < CFG.c1_max_replicas   # never the 20-pod cap
    assert all(lv < 3 for lv, _ in seen.values())
    after_peak = [seen[t] for t in range(210, 520, 5) if seen[t][0] >= 1]
    assert all(tg == 14 for _, tg in after_peak)  # no drop at the L2 -> L1 step-down
    assert seen[515] == (0, 2)                    # back to the original size at L0


def test_c1_floor_is_read_from_the_plan():
    from lifeboat.models import PlanRecord, Step, step_key
    from lifeboat.planner import c1_floor_from_plan
    k = step_key("scale_deployment", "ns", "c1-emergency")
    p = step_key("scale_deployment", "ns", "placeholder")
    rec = PlanRecord(level=2, steps={k: Step(k, "scale_deployment", "ns", "c1-emergency", {"replicas": 12}),
                                     p: Step(p, "scale_deployment", "ns", "placeholder", {"replicas": 0})})
    assert c1_floor_from_plan(rec, demo_wl()) == {"c1-emergency": 12}
    assert c1_floor_from_plan(None, demo_wl()) == {}

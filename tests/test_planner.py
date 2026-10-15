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

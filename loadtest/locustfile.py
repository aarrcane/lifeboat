"""20x surge on C1 and C2, run in-cluster by `make load` (deploy/jobs/locust.yaml).

  locust -f loadtest/locustfile.py --headless

- Two user classes, so a slow C2 does not slow down the C1 load.
- Every user tries one request per second (constant_throughput), so offered load = users.
- Every request closes its connection: a Kubernetes Service balances per connection, so
  keep-alive would pin each user to one pod and new pods would get no load.
- Every request carries X-Request-Start (epoch seconds), so the web app's latency histogram
  includes the time spent queued (workloads/webapp/app.py).
- A request counts as failed when it is not a 200 or took longer than the 0.5 s target, so
  Locust's "failures" means "missed the target".
- Stages come from env STAGES ("end_s:users,..."). Stats are reset when the surge starts and
  frozen when it ends, then printed as one `RESULT {...}` line per endpoint at exit
  (read by scripts/run_experiment.py with `kubectl logs job/locust`).
"""
import json
import os
import time

from locust import HttpUser, LoadTestShape, constant_throughput, events, task

BASE = {"C1": os.getenv("C1_URL", "http://c1-emergency.lifeboat-demo"),
        "C2": os.getenv("C2_URL", "http://c2-portal.lifeboat-demo")}
DEFAULT_STAGES = "150:10,330:200,360:10"     # 2.5 min normal, 3 min 20x surge, 30 s cool-down
TARGET_S = float(os.getenv("TARGET_S", "0.5"))
TIMEOUT_S = float(os.getenv("TIMEOUT_S", "5"))
SPAWN_RATE = float(os.getenv("SPAWN_RATE", "50"))


def parse_stages(text: str) -> list[tuple[int, int]]:
    out = []
    for part in text.split(","):
        end, users = part.strip().split(":")
        out.append((int(float(end)), int(users)))
    return out


STAGES = parse_stages(os.getenv("STAGES", DEFAULT_STAGES))
_SURGE_USERS = max(u for _, u in STAGES)
_SURGE = next(i for i, (_, u) in enumerate(STAGES) if u == _SURGE_USERS)
_results: dict[str, dict] = {}


def _get(user, tier: str) -> None:
    t0 = time.perf_counter()
    with user.client.get(BASE[tier] + "/", name=tier, timeout=TIMEOUT_S, catch_response=True,
                         headers={"Connection": "close", "X-Request-Start": f"{time.time():.6f}"}) as r:
        took = time.perf_counter() - t0
        if r.status_code != 200:
            r.failure(f"status {r.status_code}")
        elif took > TARGET_S:
            r.failure(f"slow: {took * 1000:.0f} ms > {TARGET_S * 1000:.0f} ms")
        else:
            r.success()


class EmergencyUser(HttpUser):
    weight = 3
    host = BASE["C1"]
    wait_time = constant_throughput(1)

    @task
    def emergency(self):
        _get(self, "C1")


class PortalUser(HttpUser):
    weight = 2
    host = BASE["C2"]
    wait_time = constant_throughput(1)

    @task
    def portal(self):
        _get(self, "C2")


def _snapshot(stats) -> dict[str, dict]:
    out = {}
    for name in ("C1", "C2"):
        e = stats.get(name, "GET")
        out[name] = {"name": name, "requests": e.num_requests, "failures": e.num_failures,
                     "p50_ms": round(e.get_response_time_percentile(0.5) or 0, 1),
                     "p95_ms": round(e.get_response_time_percentile(0.95) or 0, 1)}
    return out


class Surge(LoadTestShape):
    """Users per stage from STAGES; the stage with the most users is the surge."""
    _stage = -1

    def tick(self):
        t = self.get_run_time()
        for i, (end, users) in enumerate(STAGES):
            if t < end:
                if i != self._stage:
                    if i == _SURGE:
                        self.runner.stats.reset_all()         # final numbers cover the surge only
                        print(f"SURGE_START t={t:.1f}", flush=True)
                    elif self._stage == _SURGE:
                        _results.update(_snapshot(self.runner.stats))
                        print(f"SURGE_END t={t:.1f}", flush=True)
                    self._stage = i
                return users, SPAWN_RATE
        return None


@events.quitting.add_listener
def _print_results(environment, **_kw):
    res = _results or _snapshot(environment.stats)              # surge never ended: whatever ran
    for name in ("C1", "C2"):
        print("RESULT " + json.dumps(res[name]), flush=True)

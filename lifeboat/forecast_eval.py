"""Offline evaluation of the S3 traffic forecaster (P6). Pure logic, no cluster.

Builds traffic traces the way Prometheus would see them (`sum(rate(http_requests_total[1m]))`
sampled every `sample_s`), replays them through `TrafficForecaster`, and measures:

* lead time: how many seconds before the *measured* traffic crosses X / Y times normal
  the forecaster already votes L1 / L2 (positive = forecast warned first),
* false votes: samples voting L1+ / L2 on traffic that never surges,
* flips: how often the S3 vote changes on a no-crisis trace.

Used by scripts/demo_p6.py and tests/test_p6_observability.py.
"""
from __future__ import annotations

import dataclasses
import itertools
import random
from typing import Callable, Iterable, Optional

from .forecast import TrafficForecaster
from .models import Level

NORMAL_RPS = 10.0
RATE_WINDOW_S = 60          # PromQL rate(...[1m])

# Locust shape from loadtest/locustfile.py: (end_s, users). ~1 request/s per user (wait 0.5-1.5 s).
LOCUST_STAGES = ((120, 10), (180, 200), (480, 200), (600, 10))
LOCUST_SPAWN_RATE = 50.0


# ---------------------------------------------------------------- traces (requests per second)
def locust_surge_rps(t: float) -> float:
    """True request rate of the P5 Locust 20x surge: 10 users -> 200 users at 50 users/s."""
    prev_end, users = 0.0, 10.0
    for end, target in LOCUST_STAGES:
        step = min(abs(target - users), (t - prev_end) * LOCUST_SPAWN_RATE)
        now = users + step if target >= users else users - step
        if t < end:
            return now
        prev_end, users = float(end), float(target)
    return users


def gradual_surge_rps(t: float) -> float:
    """Flood-style build-up: 5 min normal, traffic climbs over ~8 min to 20x, holds, decays.
    Citizens start checking the portals as rain warnings spread, before the peak hits."""
    if t < 300:
        return NORMAL_RPS
    if t < 780:
        frac = (t - 300) / 480.0
        return NORMAL_RPS * (20.0 ** (frac ** 1.5))           # slow start, then steep
    if t < 1080:
        return NORMAL_RPS * 20.0
    if t < 1380:
        return NORMAL_RPS * 20.0 * (1 - (t - 1080) / 300.0) + NORMAL_RPS * (t - 1080) / 300.0
    return NORMAL_RPS


def no_crisis_rps(t: float, seed: int = 7) -> float:
    """Normal day: slow wiggle, plus two short bursts (a bot, a news link) that are not crises."""
    import math
    base = NORMAL_RPS * (1 + 0.25 * math.sin(t / 400.0))
    if 400 <= t < 430 or 1100 <= t < 1140:
        base *= 2.5
    return base


def measured(trace: Callable[[float], float], duration_s: int, sample_s: float,
             noise: float = 0.10, seed: int = 1) -> list[tuple[float, float]]:
    """What Monitor.cluster_rps() returns every `sample_s`: the 1-minute trailing average
    of the true rate (PromQL rate()[1m]), with multiplicative noise from request jitter."""
    rng = random.Random(seed)
    per_second = [trace(float(s)) for s in range(duration_s + 1)]
    out, t = [], 0.0
    while t <= duration_s:
        i = int(t)
        lo = max(0, i - RATE_WINDOW_S + 1)
        avg = sum(per_second[lo:i + 1]) / (i + 1 - lo)
        out.append((t, max(0.0, avg * (1 + rng.uniform(-noise, noise)))))
        t += sample_s
    return out


# ---------------------------------------------------------------- replay + scoring
@dataclasses.dataclass
class Params:
    alpha: float = 0.5
    beta: float = 0.3
    horizon: int = 12
    x: float = 2.0
    y: float = 4.0
    warmup: int = 8
    sample_s: float = 15.0


@dataclasses.dataclass
class Run:
    times: list[float]
    rps: list[float]
    predicted: list[float]
    ratio: list[float]
    levels: list[int]
    baseline: list[float]


def replay(samples: Iterable[tuple[float, float]], p: Params) -> Run:
    f = TrafficForecaster(p.alpha, p.beta, p.horizon, p.x, p.y, p.warmup)
    run = Run([], [], [], [], [], [])
    for t, rps in samples:
        f.observe(rps)
        run.times.append(t)
        run.rps.append(rps)
        run.predicted.append(f.predicted())
        run.ratio.append(f.ratio())
        run.levels.append(int(f.level()))
        run.baseline.append(f.baseline)
    return run


def _first(times, flags) -> Optional[float]:
    return next((t for t, f in zip(times, flags) if f), None)


@dataclasses.dataclass
class SurgeScore:
    forecast_l1_s: Optional[float]
    forecast_l2_s: Optional[float]
    measured_l1_s: Optional[float]      # first sample where measured RPS >= X * normal
    measured_l2_s: Optional[float]      # first sample where measured RPS >= Y * normal
    lead_l1_s: Optional[float]
    lead_l2_s: Optional[float]


def score_surge(run: Run, p: Params, normal: float = NORMAL_RPS) -> SurgeScore:
    f1 = _first(run.times, [lv >= Level.L1 for lv in run.levels])
    f2 = _first(run.times, [lv >= Level.L2 for lv in run.levels])
    m1 = _first(run.times, [r >= p.x * normal for r in run.rps])
    m2 = _first(run.times, [r >= p.y * normal for r in run.rps])
    lead = lambda m, f: None if m is None or f is None else m - f
    return SurgeScore(f1, f2, m1, m2, lead(m1, f1), lead(m2, f2))


@dataclasses.dataclass
class QuietScore:
    false_l1_samples: int
    false_l2_samples: int
    flips: int


def score_quiet(run: Run) -> QuietScore:
    flips = sum(1 for a, b in zip(run.levels, run.levels[1:]) if a != b)
    return QuietScore(sum(lv >= 1 for lv in run.levels), sum(lv >= 2 for lv in run.levels), flips)


TRACES = {
    "locust_step": (locust_surge_rps, 720),
    "gradual_flood": (gradual_surge_rps, 1500),
    "no_crisis": (no_crisis_rps, 1500),
}


def benchmark(p: Params, seed: int = 1) -> dict:
    out = {}
    for name, (trace, dur) in TRACES.items():
        run = replay(measured(trace, dur, p.sample_s, seed=seed), p)
        out[name] = score_quiet(run) if name == "no_crisis" else score_surge(run, p)
    return out


def sweep(alphas=(0.3, 0.5, 0.7), betas=(0.1, 0.3, 0.5), horizons=(4, 8, 12),
          seeds=(1, 2, 3)) -> list[dict]:
    """Grid search. A setting is 'safe' if it never votes L2 on the no-crisis trace for any seed.
    Among safe settings, rank by the average L2 lead time on the gradual flood."""
    rows = []
    for a, b, h in itertools.product(alphas, betas, horizons):
        p = Params(alpha=a, beta=b, horizon=h)
        leads, false2, false1, flips = [], 0, 0, 0
        for s in seeds:
            r = benchmark(p, seed=s)
            leads.append(r["gradual_flood"].lead_l2_s or 0.0)
            false2 += r["no_crisis"].false_l2_samples
            false1 += r["no_crisis"].false_l1_samples
            flips += r["no_crisis"].flips
        rows.append({"alpha": a, "beta": b, "horizon": h,
                     "lead_l2_s": sum(leads) / len(leads),
                     "false_l1": false1, "false_l2": false2, "flips": flips,
                     "safe": false2 == 0})
    rows.sort(key=lambda r: (not r["safe"], -r["lead_l2_s"], r["false_l1"]))
    return rows

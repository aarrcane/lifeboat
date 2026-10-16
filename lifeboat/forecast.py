"""Holt linear-trend forecaster (FPP3 s8.2) + traffic-ratio -> alert level (source S3)."""
from __future__ import annotations

from typing import Optional

from .models import Level


class Holt:
    def __init__(self, alpha: float = 0.5, beta: float = 0.3):
        self.alpha, self.beta = alpha, beta
        self.level: Optional[float] = None
        self.trend: float = 0.0

    def update(self, x: float) -> None:
        if self.level is None:
            self.level, self.trend = x, 0.0
            return
        prev = self.level
        self.level = self.alpha * x + (1 - self.alpha) * (prev + self.trend)
        self.trend = self.beta * (self.level - prev) + (1 - self.beta) * self.trend

    def forecast(self, h: int) -> float:
        if self.level is None:
            return 0.0
        return max(0.0, self.level + h * self.trend)


class TrafficForecaster:
    """Tracks cluster-wide RPS against a slow baseline of normal traffic.

    The baseline is seeded from the first `warmup` samples (no vote until then) and
    afterwards only learns from samples the forecaster itself judges normal. It must not
    depend on the voter's level: our own L1 vote would freeze it at its seed forever.
    """

    def __init__(self, alpha: float, beta: float, horizon: int, x: float, y: float,
                 warmup: int = 8, learn_rate: float = 0.02):
        self.holt = Holt(alpha, beta)
        self.horizon, self.x, self.y = horizon, x, y
        self.warmup, self.learn_rate = max(1, warmup), learn_rate
        self.samples = 0
        self._warm_sum = 0.0
        self.baseline = 1.0

    @property
    def warming_up(self) -> bool:
        return self.samples < self.warmup

    def observe(self, rps: float) -> None:
        self.holt.update(rps)
        self.samples += 1
        if self.samples <= self.warmup:                       # mean of the first samples
            self._warm_sum += rps
            self.baseline = max(1.0, self._warm_sum / self.samples)
        elif self.ratio() < self.x:                           # only learn from normal traffic
            self.baseline = max(1.0, (1 - self.learn_rate) * self.baseline + self.learn_rate * rps)

    def predicted(self) -> float:
        return self.holt.forecast(self.horizon)

    def ratio(self) -> float:
        return self.predicted() / self.baseline

    def level(self) -> Level:
        if self.warming_up:
            return Level.L0                                   # no baseline yet => no vote
        r = self.ratio()
        return Level.L2 if r >= self.y else Level.L1 if r >= self.x else Level.L0

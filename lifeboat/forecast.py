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
    """Tracks cluster-wide RPS; a slow baseline only learns while things are normal."""

    def __init__(self, alpha: float, beta: float, horizon: int, x: float, y: float):
        self.holt = Holt(alpha, beta)
        self.horizon, self.x, self.y = horizon, x, y
        self.baseline = 1.0

    def observe(self, rps: float, level: Level) -> None:
        self.holt.update(rps)
        if level == Level.L0:
            self.baseline = max(1.0, 0.98 * self.baseline + 0.02 * rps)

    def predicted(self) -> float:
        return self.holt.forecast(self.horizon)

    def ratio(self) -> float:
        return self.predicted() / self.baseline

    def level(self) -> Level:
        r = self.ratio()
        return Level.L2 if r >= self.y else Level.L1 if r >= self.x else Level.L0

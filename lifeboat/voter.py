"""Algorithm 1 - voting. Pure logic: no I/O, easy to unit test."""
from __future__ import annotations

from typing import Iterable, Optional

from .models import Level, Reading
from .signing import verify


class Voter:
    def __init__(self, secret: str, calm_seconds: float, window_s: float = 600.0,
                 naive: bool = False):
        self.secret = secret
        self.calm_seconds = calm_seconds
        self.window_s = window_s
        self.naive = naive
        self.level: Level = Level.L0
        self.override: Optional[Level] = None
        self._calm_since: Optional[float] = None

    def set_override(self, level: Optional[Level]) -> None:
        """Human override locks the level and pauses automated voting until cleared."""
        self.override = level
        if level is not None:
            self.level = level
        self._calm_since = None

    def valid_readings(self, readings: Iterable[Reading], now: float) -> list[Reading]:
        # expired / unsigned / too old => treated as missing
        return [r for r in readings
                if r.expires_at > now and now - r.ts <= self.window_s and verify(r, self.secret)]

    def target(self, readings: Iterable[Reading], now: float, c1_struggling: bool) -> Level:
        valid = self.valid_readings(readings, now)
        # one (latest) reading per source so one noisy feed can't vote twice
        latest: dict[str, Reading] = {}
        for r in valid:
            if r.source not in latest or r.ts > latest[r.source].ts:
                latest[r.source] = r
        levels = sorted((r.level for r in latest.values()), reverse=True)
        agreed = levels[1] if len(levels) >= 2 else 0          # highest level >=2 sources support
        if c1_struggling:
            return Level.L3                                    # measured, no vote
        if self.naive and levels and levels[0] >= Level.L1:
            return Level.L2                                    # naive baseline: any 1 source -> L2
        if agreed >= Level.L2:
            return Level.L2                                    # sources only vote up to L2
        if levels and levels[0] >= Level.L1:
            return Level.L1                                    # cheap, easy to undo
        return Level.L0

    def evaluate(self, readings: Iterable[Reading], now: float, c1_struggling: bool) -> Level:
        if self.override is not None:
            self.level = self.override
            return self.level
        target = self.target(readings, now, c1_struggling)
        if target > self.level:
            self.level = target                                # up: immediately
            self._calm_since = None
        elif target < self.level:
            if self.naive or self.calm_seconds <= 0:
                self.level = target                            # naive: no calm-period hysteresis
                self._calm_since = None
            elif self._calm_since is None:
                self._calm_since = now
            elif now - self._calm_since >= self.calm_seconds:
                self.level = Level(self.level - 1)             # down: one level per calm period
                self._calm_since = now
        else:
            self._calm_since = None
        return self.level

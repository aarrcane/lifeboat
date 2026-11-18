"""Sense -> decide -> act loop (structure adapted from the GreenScale design, report ref [13])."""
from __future__ import annotations

import logging
import threading
import time

from kubernetes import config as kconfig
from prometheus_client import start_http_server

from . import checkpoint, discovery, ingest, metrics
from .config import Config
from .executor import Executor
from .forecast import TrafficForecaster
from .k8s_actions import K8sActions
from .k8s_plan_store import K8sPlanStore
from .lease import LeaseElector
from .models import Level, Reading
from .override_store import NONE as NO_OVERRIDE, K8sOverrideStore
from .planner import c1_floor_from_plan, c1_sizing_rps, make_plan
from .signing import sign
from .voter import Voter

log = logging.getLogger("lifeboat")


class Lifeboat:
    def __init__(self, cfg: Config):
        try:
            kconfig.load_incluster_config()
        except kconfig.ConfigException:
            kconfig.load_kube_config()
        self.cfg = cfg
        self.cp = checkpoint.from_env()
        self.monitor = __import__("lifeboat.monitor", fromlist=["Monitor"]).Monitor(cfg)
        self.book = ingest.ReadingBook()
        self.voter = Voter(cfg.hmac_secret, cfg.calm_minutes * 60, cfg.vote_window_s)
        self.traffic = TrafficForecaster(cfg.holt_alpha, cfg.holt_beta, cfg.forecast_horizon_steps,
                                         cfg.x_factor, cfg.y_factor, cfg.forecast_warmup_samples)
        # C1 traffic: prediction plus a baseline of normal C1 traffic (for the L2 pre-size rule)
        self.c1_traffic = TrafficForecaster(cfg.holt_alpha, cfg.holt_beta, cfg.forecast_horizon_steps,
                                            cfg.x_factor, cfg.y_factor, cfg.forecast_warmup_samples)
        self._last_c1_rps: float | None = None
        self.store = K8sPlanStore(cfg.system_namespace, cfg.plan_name)
        self.overrides = K8sOverrideStore(cfg.system_namespace)
        self._override_seen = NO_OVERRIDE
        self._last_loop = time.time()
        self.executor = Executor(
            self.store, K8sActions(cfg, self.cp),
            LeaseElector(cfg.identity, cfg.lease_name, cfg.system_namespace, cfg.lease_duration_s))
        self._last_sample = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle
    def start(self) -> None:
        start_http_server(self.cfg.metrics_port)
        ingest.serve(self.cfg.ingest_port, self.cfg.hmac_secret, self.book, self._override,
                     healthy=self.healthy)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lifeboat-loop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _override(self, level, who: str) -> None:
        """Store the override where every replica reads it (raises if it could not be stored)."""
        log.warning("HUMAN OVERRIDE by %s -> %s", who, level)
        ov = self.overrides.set(level, who)
        metrics.OVERRIDES.inc()
        self._apply_override(ov)

    def _apply_override(self, ov) -> None:
        if ov != self._override_seen:
            if ov.level is not None or self.voter.override is not None:
                log.warning("override now %s (set by %s)", ov.level, ov.who or "unknown")
                self.voter.set_override(ov.level)
            self._override_seen = ov

    def _sync_override(self) -> None:
        try:
            self._apply_override(self.overrides.get())
        except Exception as e:                          # keep the last known override
            log.warning("could not read the override: %s", e)

    def healthy(self) -> bool:
        """Liveness: the loop finished a tick recently (a hung API call would stop it)."""
        age = time.time() - self._last_loop
        metrics.LOOP_AGE.set(age)
        return age < self.cfg.loop_stall_s

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.step()
            except Exception:
                log.exception("tick failed (will retry)")
            self._last_loop = time.time()
            took = self._last_loop - t0
            metrics.TICK_SECONDS.set(took)
            if took > self.cfg.lease_duration_s / 2:
                log.warning("tick took %.1f s, close to the %d s lease", took, self.cfg.lease_duration_s)
            self._stop.wait(self.cfg.tick_s)

    # ---- one tick
    def step(self, now: float | None = None) -> None:
        cfg, now = self.cfg, now or time.time()
        self._sync_override()
        struggling = self.monitor.c1_struggling()

        if now - self._last_sample >= cfg.forecast_sample_s:          # S3: forecast source
            self._last_sample = now
            rps, c1 = self.monitor.cluster_rps_sample(), self.monitor.c1_rps_sample()
            if rps is not None:                                        # no data => skip, don't learn 0
                self.traffic.observe(rps)
            if c1 is not None:
                self.c1_traffic.observe(c1)
                self._last_c1_rps = c1
            if rps is not None:
                # Vote only from a fresh sample. During a monitoring outage the last vote simply
                # expires (forecast_ttl_s) and S3 counts as silent, so an outage can never keep
                # an old L2 forecast alive.
                self.book.add(sign(Reading("S3", int(self.traffic.level()), now,
                                           now + cfg.forecast_ttl_s), cfg.hmac_secret), now)
                metrics.FORECAST_RATIO.set(self.traffic.ratio())

        rec = self.store.get()
        if rec is not None and not self.executor.is_active:            # standby: plan level is the truth
            self.voter.follow(Level(rec.level))
        readings = self.book.snapshot() if cfg.early_warning else []   # B2: reactive only
        level = self.voter.evaluate(readings, now, struggling)

        workloads = discovery.list_workloads(cfg, self.cp)
        discovery.apply_originals(workloads, rec)                      # pre-triage view
        applied = sum(s.params.get("delta_millis", 0) for s in (rec.steps.values() if rec else []) if s.done)
        baseline_free = discovery.free_millis(cfg.api_timeout_s) - applied
        c1_rps = c1_sizing_rps(level, self.c1_traffic.predicted(), self._last_c1_rps,
                               self.c1_traffic.baseline, cfg)
        desired = make_plan(level, workloads, c1_rps, baseline_free, cfg, now,
                            c1_floor=c1_floor_from_plan(rec, workloads))

        self.executor.tick(int(level), desired)
        self._export(level, struggling, readings, now)

    def _export(self, level, struggling, readings, now) -> None:
        metrics.LEVEL.set(int(level))
        metrics.C1_STRUGGLING.set(int(struggling))
        metrics.IS_ACTIVE.set(int(self.executor.is_active))
        latest: dict[str, Reading] = {}
        for r in self.voter.valid_readings(readings, now):
            if r.source not in latest or r.ts > latest[r.source].ts:
                latest[r.source] = r
        # no valid reading => 0, so the dashboard shows a feed that went silent
        for src in sorted({"S1", "S2", "S3"} | set(latest)):
            metrics.SOURCE_LEVEL.labels(src).set(latest[src].level if src in latest else 0)
        rec = self.store.get()
        if rec:
            metrics.TAKEOVER.set(rec.takeover)
            done = sum(s.done for s in rec.steps.values())
            metrics.PLAN_STEPS.labels("done").set(done)
            metrics.PLAN_STEPS.labels("pending").set(len(rec.steps) - done)

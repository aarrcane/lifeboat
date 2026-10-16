"""Sense -> decide -> act loop (borrowed from the GreenScale design)."""
from __future__ import annotations

import logging
import threading
import time

from kubernetes import client, config as kconfig
from prometheus_client import start_http_server

from . import checkpoint, discovery, ingest, metrics
from .config import Config
from .executor import Executor
from .forecast import Holt, TrafficForecaster
from .k8s_actions import K8sActions
from .k8s_plan_store import K8sPlanStore
from .lease import LeaseElector
from .models import Level, Reading
from .planner import make_plan
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
        self.c1_holt = Holt(cfg.holt_alpha, cfg.holt_beta)
        self.store = K8sPlanStore(cfg.system_namespace, cfg.plan_name)
        self.executor = Executor(
            self.store, K8sActions(cfg, self.cp),
            LeaseElector(cfg.identity, cfg.lease_name, cfg.system_namespace, cfg.lease_duration_s))
        self._last_sample = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle
    def start(self) -> None:
        start_http_server(self.cfg.metrics_port)
        ingest.serve(self.cfg.ingest_port, self.cfg.hmac_secret, self.book, self._override)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lifeboat-loop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _override(self, level, who: str) -> None:
        log.warning("HUMAN OVERRIDE by %s -> %s", who, level)
        metrics.OVERRIDES.inc()
        self.voter.set_override(level)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("tick failed (will retry)")
            self._stop.wait(self.cfg.tick_s)

    # ---- one tick
    def step(self, now: float | None = None) -> None:
        cfg, now = self.cfg, now or time.time()
        struggling = self.monitor.c1_struggling()

        if now - self._last_sample >= cfg.forecast_sample_s:          # S3: forecast source
            self._last_sample = now
            self.traffic.observe(self.monitor.cluster_rps())
            self.c1_holt.update(self.monitor.c1_rps())
            self.book.add(sign(Reading("S3", int(self.traffic.level()), now,
                                       now + cfg.forecast_ttl_s), cfg.hmac_secret))
            metrics.FORECAST_RATIO.set(self.traffic.ratio())

        readings = self.book.snapshot() if cfg.early_warning else []   # B2: reactive only
        level = self.voter.evaluate(readings, now, struggling)

        rec = self.store.get()
        workloads = discovery.list_workloads(cfg, self.cp)
        discovery.apply_originals(workloads, rec)                      # pre-triage view
        applied = sum(s.params.get("delta_millis", 0) for s in (rec.steps.values() if rec else []) if s.done)
        baseline_free = discovery.free_millis() - applied
        c1_forecast = self.c1_holt.forecast(cfg.forecast_horizon_steps)
        desired = make_plan(level, workloads, c1_forecast, baseline_free, cfg, now)

        self.executor.tick(int(level), desired)
        self._export(level, struggling, readings, now)

    def _export(self, level, struggling, readings, now) -> None:
        metrics.LEVEL.set(int(level))
        metrics.C1_STRUGGLING.set(int(struggling))
        metrics.IS_ACTIVE.set(int(self.executor.is_active))
        for r in self.voter.valid_readings(readings, now):
            metrics.SOURCE_LEVEL.labels(r.source).set(r.level)
        rec = self.store.get()
        if rec:
            metrics.TAKEOVER.set(rec.takeover)
            done = sum(s.done for s in rec.steps.values())
            metrics.PLAN_STEPS.labels("done").set(done)
            metrics.PLAN_STEPS.labels("pending").set(len(rec.steps) - done)

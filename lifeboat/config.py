"""All tunables in one place. Override any field with env var LIFEBOAT_<FIELD_UPPERCASE>."""
from __future__ import annotations

import dataclasses
import os
import socket


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclasses.dataclass
class Config:
    # --- where things live
    system_namespace: str = "lifeboat"
    workload_namespace: str = "lifeboat-demo"
    plan_name: str = "current"
    c2_configmap: str = "c2-features"
    identity: str = socket.gethostname()
    lease_name: str = "lifeboat-executor"
    lease_duration_s: int = 15

    # --- security / ingest
    hmac_secret: str = "dev-secret-change-me"
    ingest_port: int = 8081
    metrics_port: int = 9100

    # --- voter (Appendix B). N, X, Y still need tuning (proposal says so).
    early_warning: bool = True           # False => baseline B2 (reactive only)
    calm_minutes: float = 5.0            # N
    vote_window_s: float = 600.0         # "2 of 3 within a short time window"
    x_factor: float = 2.0                # forecast traffic >= X * normal -> L1
    y_factor: float = 4.0                # forecast traffic >= Y * normal -> L2
    pub_ttl_s: float = 150.0             # PUB updates every 2 min
    nea_ttl_s: float = 330.0             # NEA updates every 5 min
    forecast_ttl_s: float = 60.0

    # --- C1 health (L3 trigger)
    c1_latency_target_s: float = 0.5
    prometheus_url: str = "http://prometheus-operated.monitoring.svc:9090"
    q_cluster_rps: str = "sum(rate(http_requests_total[1m]))"
    q_c1_rps: str = 'sum(rate(http_requests_total{tier="C1"}[1m]))'
    q_c1_p95: str = (
        'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
        '{tier="C1"}[1m])) by (le))'
    )

    # --- forecasting (Holt linear trend)
    forecast_sample_s: float = 15.0
    forecast_horizon_steps: int = 12     # 12 * 15 s = 3 minutes ahead
    holt_alpha: float = 0.5
    holt_beta: float = 0.3
    forecast_warmup_samples: int = 8     # 8 * 15 s = 2 min to learn normal traffic

    # --- planner
    cost_millis_per_rps: float = 20.0    # CPU millicores needed per request/s on C1
    margin_millis: float = 500.0
    l1_extra_c1_pods: int = 1
    l3_extra_c1_pods: int = 3
    c1_max_replicas: int = 20

    # --- executor / loop
    tick_s: float = 5.0
    checkpoint_wait_s: float = 6.0       # ONE wait per tick for all pauses; kept < lease_duration_s / 2

    # --- checkpoints
    checkpoint_backend: str = "file"     # "file" | "s3"
    checkpoint_dir: str = "/tmp/lifeboat-checkpoints"
    s3_bucket: str = "lifeboat-checkpoints"
    s3_endpoint: str = ""                # set for MinIO on laptops

    @classmethod
    def from_env(cls) -> "Config":
        kwargs = {}
        for f in dataclasses.fields(cls):
            raw = os.getenv("LIFEBOAT_" + f.name.upper())
            if raw is None:
                continue
            default = f.default
            kwargs[f.name] = _bool(raw) if isinstance(default, bool) else type(default)(raw)
        return cls(**kwargs)

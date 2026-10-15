from prometheus_client import Counter, Gauge

LEVEL = Gauge("lifeboat_level", "Current alert level 0-3")
IS_ACTIVE = Gauge("lifeboat_is_active_executor", "1 if this replica holds the Lease")
TAKEOVER = Gauge("lifeboat_takeover_number", "Takeover number seen by this replica")
C1_STRUGGLING = Gauge("lifeboat_c1_struggling", "1 if C1 misses its target")
SOURCE_LEVEL = Gauge("lifeboat_source_level", "Latest valid level per source", ["source"])
FORECAST_RATIO = Gauge("lifeboat_forecast_ratio", "Forecast traffic / baseline")
PLAN_STEPS = Gauge("lifeboat_plan_steps", "Steps in TriagePlan", ["state"])
BAD_READINGS = Counter("lifeboat_rejected_readings_total", "Readings rejected at ingest", ["reason"])
OVERRIDES = Counter("lifeboat_overrides_total", "Human overrides")

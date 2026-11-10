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

# Export every rejection reason at 0 from the start. A labelled counter only appears on its
# first increment, and increase() needs a sample before it, so without this the first forged
# reading would never fire the LifeboatForgedReadings alert (found by scripts/p6_live_check.py).
for _reason in ("malformed", "bad_signature", "expired"):
    BAD_READINGS.labels(_reason)

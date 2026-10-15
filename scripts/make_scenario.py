"""Generate a synthetic flash-flood scenario for feeds.replay (t in seconds)."""
import json

ev = []
for t in range(0, 1800, 120):   # PUB every 2 min
    ev.append({"t": t, "source": "S1", "level": 0 if t < 600 else 2 if t < 1200 else 0})
for t in range(0, 1800, 300):   # NEA every 5 min
    ev.append({"t": t, "source": "S2", "level": 0 if t < 300 else 1 if t < 600 else 2 if t < 1200 else 0})
with open("data/scenario_flash_flood.jsonl", "w") as f:
    f.write("\n".join(json.dumps(e) for e in ev) + "\n")
print(len(ev), "events")

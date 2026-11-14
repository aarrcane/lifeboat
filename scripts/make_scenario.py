"""Generate synthetic replay scenarios for feeds.replay and E4 alert-accuracy tests (t in seconds).

Scenarios written to data/:
  1. scenario_flash_flood.jsonl  - True crisis: S2 hits L1 at t=300s, S1+S2 agree on L2 at t=600..1080s
  2. scenario_noisy.jsonl        - No crisis (flapping S1 sensor spikes to L2 at t=240, 480, 720s; S2=L0)
  3. scenario_conflicting.jsonl  - No crisis (S1=L2 at t=240s then clears; S2=L2 at t=900s while S1=L0)
  4. scenario_no_crisis.jsonl    - No crisis (passing light rain: S2=L1 at t=600s; S1=L0 throughout)
  5. scenario_experiment.jsonl   - E1/E2/E3 runs at speed 1 (scripts/run_experiment.py): S2=L1 at
                                   t=75s, S1+S2=L2 at t=90s repeated every 60 s until 330s, both
                                   L0 at 330s. The Locust surge starts at t=150s, 60 s after the
                                   L2 warning, and ends at 330s (loadtest/locustfile.py).
"""
import json
import os


def build_flash_flood() -> list[dict]:
    ev = []
    for t in range(0, 1800, 120):   # PUB every 2 min
        ev.append({"t": t, "source": "S1", "level": 0 if t < 600 else 2 if t < 1200 else 0})
    for t in range(0, 1800, 300):   # NEA every 5 min
        ev.append({"t": t, "source": "S2", "level": 0 if t < 300 else 1 if t < 600 else 2 if t < 1200 else 0})
    return ev


def build_noisy() -> list[dict]:
    ev = []
    noisy_spikes = {240, 480, 720}
    for t in range(0, 1800, 120):   # PUB glitches to L2 every 4 min; clears on alternate polls
        ev.append({"t": t, "source": "S1", "level": 2 if t in noisy_spikes else 0})
    for t in range(0, 1800, 300):   # NEA sees dry weather (L0)
        ev.append({"t": t, "source": "S2", "level": 0})
    return ev


def build_conflicting() -> list[dict]:
    ev = []
    for t in range(0, 1800, 120):   # PUB reports L2 early at t=240s, then back to L0
        ev.append({"t": t, "source": "S1", "level": 2 if t == 240 else 0})
    for t in range(0, 1800, 300):   # NEA reports L2 much later at t=900s when PUB is L0
        ev.append({"t": t, "source": "S2", "level": 2 if t == 900 else 0})
    return ev


def build_no_crisis() -> list[dict]:
    ev = []
    for t in range(0, 1800, 120):   # PUB stays L0 all day
        ev.append({"t": t, "source": "S1", "level": 0})
    for t in range(0, 1800, 300):   # NEA sees one brief L1 shower at t=600s
        ev.append({"t": t, "source": "S2", "level": 1 if t == 600 else 0})
    return ev


def build_experiment() -> list[dict]:
    ev = [{"t": 0, "source": "S1", "level": 0}, {"t": 0, "source": "S2", "level": 0},
          {"t": 75, "source": "S2", "level": 1}]                       # heavy rain
    for t in range(90, 330, 60):                                       # flood alert, repeated
        ev += [{"t": t, "source": "S1", "level": 2}, {"t": t, "source": "S2", "level": 2}]
    ev += [{"t": 330, "source": "S1", "level": 0}, {"t": 330, "source": "S2", "level": 0}]
    return ev


SCENARIOS = {
    "data/scenario_flash_flood.jsonl": build_flash_flood,
    "data/scenario_noisy.jsonl": build_noisy,
    "data/scenario_conflicting.jsonl": build_conflicting,
    "data/scenario_no_crisis.jsonl": build_no_crisis,
    "data/scenario_experiment.jsonl": build_experiment,
}


def main() -> None:
    os.makedirs("data", exist_ok=True)
    for path, builder in SCENARIOS.items():
        ev = builder()
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(e) for e in ev) + "\n")
        print(f"{path}: {len(ev)} events")


if __name__ == "__main__":
    main()

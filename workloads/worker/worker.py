"""C3 analytics / C4 backup stand-in: resumable work with the Lifeboat checkpoint contract.
 - restores from latest checkpoint on start
 - checkpoints every CHECKPOINT_EVERY seconds
 - saves immediately when Lifeboat writes requests/<name>.json
 - saves on SIGTERM (Job suspend => pod deleted with grace period), unless SAVE_ON_TERM=false
   (the case of an app that does not handle SIGTERM)
 - exits 143 on SIGTERM while work is left: for a Job, exit 0 means success, so a preempted or
   deleted pod must not mark the Job Complete with the work unfinished. Exit 0 only when done.
 - writes lifecycle events (start, term, done) through the store for E3 (lifeboat/e3_collect.py),
   since pod logs disappear when a Job is suspended
"""
import os
import signal
import sys
import time

from checkpoint import from_env

NAME = os.environ["WORK_NAME"]
TOTAL = int(os.getenv("TOTAL_UNITS", "100000"))
UNIT_S = float(os.getenv("UNIT_SECONDS", "0.05"))
EVERY = float(os.getenv("CHECKPOINT_EVERY", "30"))
SAVE_ON_TERM = os.getenv("SAVE_ON_TERM", "true").strip().lower() not in ("0", "false", "no", "off")

store = from_env()
state = store.load(NAME) or {"unit": 0}
print(f"[{NAME}] starting at unit {state['unit']}/{TOTAL}", flush=True)


def event(kind: str) -> None:
    try:
        store.record_event(NAME, kind, state["unit"])
    except Exception as e:                         # E3 bookkeeping must never stop the work
        print(f"[{NAME}] could not record {kind} event: {e}", flush=True)


event("start")
seen_request = store.last_request_ts(NAME)
last_cp = time.time()
last_poll = 0.0


def save(reason: str) -> None:
    store.save(NAME, state)
    print(f"[{NAME}] checkpoint ({reason}) at unit {state['unit']}", flush=True)


def on_term(*_):
    event("term")
    if SAVE_ON_TERM:
        save("SIGTERM")
    else:
        print(f"[{NAME}] SIGTERM at unit {state['unit']}, not saving (SAVE_ON_TERM=false)", flush=True)
    sys.exit(0 if state["unit"] >= TOTAL else 143)


signal.signal(signal.SIGTERM, on_term)

while state["unit"] < TOTAL:
    time.sleep(UNIT_S)
    state["unit"] += 1
    now = time.time()
    req = seen_request
    if now - last_poll >= 1.0:                     # check for a "save now" request once a second
        last_poll = now
        req = store.last_request_ts(NAME)
    if req > seen_request:
        seen_request = req
        save("requested")
        last_cp = now
    elif now - last_cp >= EVERY:
        save("periodic")
        last_cp = now
save("done")
event("done")

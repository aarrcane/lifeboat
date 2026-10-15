"""Kopf entrypoint:  kopf run --standalone --namespace lifeboat -m lifeboat.operator"""
import logging

import kopf

from .config import Config
from .controller import Lifeboat

_app = None


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    global _app
    settings.peering.standalone = True            # leader election is our own Lease logic
    settings.posting.enabled = False
    logging.getLogger().setLevel(logging.INFO)
    _app = Lifeboat(Config.from_env())
    _app.start()


@kopf.on.cleanup()
def cleanup(**_):
    if _app:
        _app.stop()


@kopf.on.event("lifeboat.sit.edu", "v1", "triageplans")
def audit(event, name, spec, **_):
    """Audit trail of every TriagePlan change (useful for the report/demo)."""
    logging.getLogger("lifeboat.audit").info(
        "plan %s level=%s takeover=%s steps=%s", name, (spec or {}).get("level"),
        (spec or {}).get("takeover"), [(s["key"], s["done"]) for s in (spec or {}).get("steps", [])])

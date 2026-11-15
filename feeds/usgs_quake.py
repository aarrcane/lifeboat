"""Example extra source: earthquakes from the USGS live GeoJSON feed.

Not deployed and not used in our experiments. It shows that Lifeboat is not tied to floods:
the voter takes a signed reading from any named source, so a new hazard is one small feed like
this plus a CronJob. Pick a place (QUAKE_LAT/QUAKE_LON) and it reports:
  - L1 if a quake of magnitude >= QUAKE_L1_MAG (5.0) happened within QUAKE_RADIUS_KM (300 km)
  - L2 if one of magnitude >= QUAKE_L2_MAG (6.5) did, or USGS flags a tsunami risk for it
Feed docs: https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php
"""
import math
import os

from . import common

DEFAULT_USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
USGS_URL = os.getenv("USGS_URL", DEFAULT_USGS_URL)
SOURCE = os.getenv("QUAKE_SOURCE", "S4")
TTL_S = float(os.getenv("QUAKE_TTL_S", "330"))
LAT = float(os.getenv("QUAKE_LAT", "1.35"))         # default: Singapore
LON = float(os.getenv("QUAKE_LON", "103.82"))
RADIUS_KM = float(os.getenv("QUAKE_RADIUS_KM", "300"))
L1_MAG = float(os.getenv("QUAKE_L1_MAG", "5.0"))
L2_MAG = float(os.getenv("QUAKE_L2_MAG", "6.5"))


def distance_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def level_from_payload(payload, lat=LAT, lon=LON, radius_km=RADIUS_KM) -> int:
    level = 0
    features = payload.get("features", []) if isinstance(payload, dict) else []
    for f in features or []:
        try:
            props = f.get("properties") or {}
            qlon, qlat = f["geometry"]["coordinates"][:2]
            mag = float(props.get("mag") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if distance_km(lat, lon, float(qlat), float(qlon)) > radius_km:
            continue
        if mag >= L2_MAG or (props.get("tsunami") == 1 and mag >= L1_MAG):
            return 2
        if mag >= L1_MAG:
            level = 1
    return level


def handle(event=None, context=None):
    level = level_from_payload(common.get_json(USGS_URL))
    return {"source": SOURCE, "level": level, "sent": common.send(common.make_reading(SOURCE, level, TTL_S))}

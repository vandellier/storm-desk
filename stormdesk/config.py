"""Runtime configuration for StormDesk."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DATA_DIR = STATIC_DIR / "data"
CACHE_DIR = ROOT / ".cache"
NEXRAD_CACHE = CACHE_DIR / "nexrad"

def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


_load_dotenv()

HOST = os.environ.get("STORMDESK_HOST", "127.0.0.1")
PORT = int(os.environ.get("STORMDESK_PORT", "8000"))
CARTO_KEY = os.environ.get("STORMDESK_CARTO_KEY", "").strip()

NWS_BASE = "https://api.weather.gov"
NWS_ALERTS = f"{NWS_BASE}/alerts/active"
NWS_USER_AGENT = os.environ.get(
    "STORMDESK_UA",
    "StormDesk/1.0 (personal storm tracker; local laptop; contact@localhost)",
)
NWS_ACCEPT = "application/geo+json"
POLL_SECONDS = int(os.environ.get("STORMDESK_POLL", "120"))

RAINVIEWER_MAPS = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_HOST = "https://tilecache.rainviewer.com"
IEM_TILE = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0"
IEM_ATTR = "https://mesonet.agron.iastate.edu/geojson/nexrad_attr.py"
IEM_RIDGE_CURRENT = "https://mesonet.agron.iastate.edu/json/ridge_current.py"

# NOAA Level II archive. AWS retired public listing on noaa-nexrad-level2
# (Sept 2025); unidata-nexrad-level2 is the same dataset and key layout.
NEXRAD_BUCKETS = ("unidata-nexrad-level2", "noaa-nexrad-level2")
NEXRAD_BUCKET = NEXRAD_BUCKETS[0]
NEXRAD_REGION = "us-east-1"

TILE_CACHE_MAX = 900
TILE_CACHE_TTL = 90
ALERT_CACHE_TTL = 45
TRACK_POLL_SECONDS = 120
ROTATION_POLL_SECONDS = 180

HTTP_TIMEOUT = 20.0
TILE_TIMEOUT = 15.0
NEXRAD_TIMEOUT = 45.0

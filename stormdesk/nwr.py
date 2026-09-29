"""NOAA Weather Radio transmitters, keyed by county SAME/FIPS code.

The table is the National Weather Service county-coverage file. Refresh it
with scripts/build_nwr.py.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from . import config, geo

LISTING = "https://www.weather.gov/dsb/county_coverage?State={state}"
PROGRAM = "https://www.weather.gov/nwr"


def _fips(same: str) -> str | None:
    digits = re.sub(r"\D", "", same or "")
    if len(digits) < 5:
        return None
    return digits[-5:]


@lru_cache(maxsize=1)
def _stations() -> list[dict[str, Any]]:
    path = config.DATA_DIR / "nwr.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("stations") or [])


@lru_cache(maxsize=1)
def _by_fips() -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for station in _stations():
        seen: set[str] = set()
        for same in station.get("same") or []:
            fips = _fips(same)
            if not fips or fips in seen:
                continue
            seen.add(fips)
            index.setdefault(fips, []).append(station)
    return index


def _public(station: dict[str, Any], km: float | None, match: str) -> dict[str, Any]:
    state = station.get("state") or ""
    return {
        "call": station.get("call"),
        "mhz": station.get("mhz"),
        "name": station.get("name"),
        "location": station.get("location"),
        "state": state,
        "lat": station.get("lat"),
        "lon": station.get("lon"),
        "watts": station.get("watts"),
        "status": station.get("status") or "UNKNOWN",
        "wfo": station.get("wfo") or "",
        "same": station.get("same") or [],
        "counties": station.get("counties") or [],
        "remarks": station.get("remarks") or "",
        "km": None if km is None else round(km, 1),
        "match": match,
        "url": LISTING.format(state=state) if state else PROGRAM,
    }


def _km(lat: float | None, lon: float | None, station: dict[str, Any]) -> float | None:
    if lat is None or lon is None:
        return None
    return geo.haversine_km(lat, lon, float(station["lat"]), float(station["lon"]))


def _rank(item: dict[str, Any]) -> tuple:
    match_order = {"county": 0, "state": 1, "nearest": 2}.get(item["match"], 3)
    air = 0 if item["status"] == "NORMAL" else 1 if item["status"] == "DEGRADED" else 2
    dist = item["km"] if item["km"] is not None else 9999
    return (match_order, air, dist, item["call"] or "")


def stations_for(
    *,
    lat: float | None = None,
    lon: float | None = None,
    state: str | None = None,
    fips: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Transmitters that cover the county, then the nearest ones to the point."""
    wanted: set[str] = set()
    for raw in fips or []:
        code = _fips(raw) if raw else None
        if code:
            wanted.add(code)
    st = (state or "").strip().upper()
    index = _by_fips()
    chosen: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def add(station: dict[str, Any], match: str) -> None:
        call = station.get("call") or ""
        if not call or call in seen:
            return
        seen.add(call)
        chosen.append((match, station))

    for code in wanted:
        for station in index.get(code, []):
            add(station, "county")

    if st and not wanted:
        for station in _stations():
            if station.get("state") == st:
                add(station, "state")

    if lat is not None and lon is not None and len(chosen) < limit:
        radius = 110 if wanted or st else 180
        nearest = []
        for station in _stations():
            if (station.get("call") or "") in seen:
                continue
            dist = _km(lat, lon, station)
            if dist is None or dist > radius:
                continue
            nearest.append((dist, station))
        nearest.sort(key=lambda pair: pair[0])
        room = limit - len([1 for match, _ in chosen if match == "county"])
        for _, station in nearest[: max(3, room)]:
            add(station, "nearest")

    public = [_public(station, _km(lat, lon, station), match) for match, station in chosen]
    public.sort(key=_rank)
    return public[:limit]

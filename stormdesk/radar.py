"""RainViewer mosaic proxy, IEM velocity/track tiles, and storm-attribute tracks."""

from __future__ import annotations

import logging
import math
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config
from .hub import hub

log = logging.getLogger("stormdesk.radar")

RV_PATH_RE = re.compile(r"^/v2/radar/[A-Za-z0-9]+$")
SITE_RE = re.compile(r"^[A-Z0-9]{4}$")
PRODUCT_RE = re.compile(r"^[A-Z0-9]{3}$")
HASH_RE = re.compile(r"^[A-Za-z0-9]+$")


class TileCache:
    def __init__(self, max_items: int = config.TILE_CACHE_MAX, ttl: float = config.TILE_CACHE_TTL):
        self.max_items = max_items
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()

    def get(self, key: str) -> tuple[bytes, str] | None:
        item = self._data.get(key)
        if not item:
            return None
        ts, body, ctype = item
        if time.time() - ts > self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return body, ctype

    def put(self, key: str, body: bytes, ctype: str) -> None:
        self._data[key] = (time.time(), body, ctype)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)


class RadarService:
    def __init__(self) -> None:
        self.tiles = TileCache()
        self.frames: dict[str, Any] = {"host": config.RAINVIEWER_HOST, "past": [], "generated": None}
        self.tracks: dict[str, Any] = {"type": "FeatureCollection", "features": []}
        self.last_frames_ok: str | None = None
        self.last_tracks_ok: str | None = None
        self.last_error: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": config.NWS_USER_AGENT},
                timeout=httpx.Timeout(config.TILE_TIMEOUT),
                follow_redirects=True,
            )
        return self._client

    async def refresh_frames(self) -> dict[str, Any]:
        client = await self.client()
        try:
            resp = await client.get(config.RAINVIEWER_MAPS, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.last_error = f"rainviewer: {exc}"
            log.warning("RainViewer maps failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        past = (data.get("radar") or {}).get("past") or []
        self.frames = {
            "host": "/api/radar",
            "sourceHost": data.get("host") or config.RAINVIEWER_HOST,
            "generated": data.get("generated"),
            "past": past,
            "nowcast": (data.get("radar") or {}).get("nowcast") or [],
        }
        self.last_frames_ok = datetime.now(timezone.utc).isoformat()
        self.last_error = None
        return {"ok": True, "frames": len(past)}

    async def refresh_tracks(self) -> dict[str, Any]:
        client = await self.client()
        try:
            resp = await client.get(config.IEM_ATTR, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.last_error = f"iem tracks: {exc}"
            log.warning("IEM storm attributes failed: %s", exc)
            return {"ok": False, "error": str(exc)}

        features = []
        for feat in data.get("features") or []:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None]
            lon, lat = (coords + [None, None])[:2]
            if lat is None or lon is None:
                continue
            try:
                lat = float(lat)
                lon = float(lon)
            except (TypeError, ValueError):
                continue
            drct = _to_float(props.get("drct") if "drct" in props else props.get("DRCT"))
            sknt = _to_float(props.get("sknt") if "sknt" in props else props.get("SKNT"))
            if drct is None:
                drct = _to_float(props.get("direction"))
            if sknt is None:
                sknt = _to_float(props.get("speed"))
            track = _projected_track(lat, lon, drct, sknt)
            tvs = str(props.get("tvs") or props.get("TVS") or "N")
            meso = str(props.get("meso") or props.get("MESO") or "N")
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "stormId": props.get("storm_id") or props.get("STORM_ID"),
                        "nexrad": props.get("nexrad") or props.get("NEXRAD"),
                        "tvs": tvs,
                        "meso": meso,
                        "posh": props.get("posh") or props.get("POSH"),
                        "poh": props.get("poh") or props.get("POH"),
                        "maxDbz": props.get("max_dbz") or props.get("MAX_DBZ"),
                        "top": props.get("top") or props.get("TOP"),
                        "vil": props.get("vil") or props.get("VIL"),
                        "drct": drct,
                        "sknt": sknt,
                        "valid": props.get("valid") or props.get("VALID"),
                        "track": track,
                        "kind": _track_kind(tvs, meso),
                    },
                }
            )
        self.tracks = {
            "type": "FeatureCollection",
            "features": features,
            "generated_at": data.get("generated_at") or data.get("generation_time"),
        }
        self.last_tracks_ok = datetime.now(timezone.utc).isoformat()
        await hub.broadcast({"type": "tracks", "geojson": self.tracks, "count": len(features)})
        return {"ok": True, "count": len(features)}

    async def fetch_tile(self, url: str, cache_key: str) -> tuple[bytes, str]:
        cached = self.tiles.get(cache_key)
        if cached:
            return cached
        client = await self.client()
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "image/png")
        body = resp.content
        self.tiles.put(cache_key, body, ctype)
        return body, ctype

    def rainviewer_tile_url(self, frame_hash: str, z: int, x: int, y: int, color: int, options: str) -> str:
        host = self.frames.get("sourceHost") or config.RAINVIEWER_HOST
        return f"{host}/v2/radar/{frame_hash}/256/{z}/{x}/{y}/{color}/{options}.png"

    def iem_ridge_url(self, site: str, product: str, z: int, x: int, y: int) -> str:
        site3 = site[-3:] if len(site) == 4 else site
        return f"{config.IEM_TILE}/ridge::{site3}-{product}-0/{z}/{x}/{y}.png"

    def iem_mosaic_url(self, z: int, x: int, y: int, minutes_ago: int = 0) -> str:
        suffix = "" if minutes_ago <= 0 else f"-m{minutes_ago:02d}m"
        return f"{config.IEM_TILE}/nexrad-n0q-900913{suffix}/{z}/{x}/{y}.png"


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _track_kind(tvs: str, meso: str) -> str:
    if tvs and tvs.upper() not in {"N", "NONE", "0", ""}:
        return "tvs"
    if meso and meso.upper() not in {"N", "NONE", "0", ""}:
        return "meso"
    return "cell"


def _projected_track(lat: float, lon: float, drct: float | None, sknt: float | None) -> dict[str, Any]:
    """Build a 45-min past + 60-min future track from motion vector.

    DRCT is the direction the storm is moving toward, degrees from north.
    """
    if drct is None or sknt is None or sknt <= 0:
        return {"past": [[lon, lat]], "future": [[lon, lat]]}

    def offset(minutes: float) -> list[float]:
        km = sknt * 1.852 * (minutes / 60.0)
        north = km * math.cos(math.radians(drct)) / 111.32
        east = km * math.sin(math.radians(drct)) / (111.32 * max(math.cos(math.radians(lat)), 0.2))
        return [round(lon + east, 4), round(lat + north, 4)]

    past = [offset(m) for m in (-45, -30, -15, 0)]
    future = [offset(m) for m in (0, 15, 30, 45, 60)]
    return {"past": past, "future": future}


radar = RadarService()

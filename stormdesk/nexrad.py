"""Download NEXRAD Level II from AWS and detect rotation signatures."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import config
from .geo import nearest_nexrad, site_by_id
from .hub import hub

log = logging.getLogger("stormdesk.nexrad")

try:
    import numpy as np
    from metpy.io import Level2File

    HAS_METPY = True
except Exception:  # pragma: no cover
    HAS_METPY = False
    np = None  # type: ignore
    Level2File = None  # type: ignore


class NexradService:
    def __init__(self) -> None:
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self.last_site: str | None = None
        self.cache: dict[str, dict[str, Any]] = {}
        self.bucket: str = config.NEXRAD_BUCKETS[-1]
        self._lock = threading.Lock()
        config.NEXRAD_CACHE.mkdir(parents=True, exist_ok=True)

    def latest_object(self, site: str, when: datetime | None = None) -> tuple[str, str]:
        when = when or datetime.now(timezone.utc)
        site = site.upper()
        prefixes: list[str] = []
        for hours_back in (0, 1, 3):
            t = when - timedelta(hours=hours_back)
            prefixes.append(f"{t:%Y}/{t:%m}/{t:%d}/{site}/{site}{t:%Y%m%d}_{t:%H}")
        for delta_days in (0, 1):
            day = when - timedelta(days=delta_days)
            prefixes.append(f"{day:%Y}/{day:%m}/{day:%d}/{site}/")

        last_error = None
        for bucket in config.NEXRAD_BUCKETS:
            for prefix in prefixes:
                try:
                    keys = _list_keys(bucket, prefix)
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code in (401, 403, 404):
                        break
                    continue
                except Exception as exc:
                    last_error = exc
                    continue
                if keys:
                    self.bucket = bucket
                    return bucket, keys[-1]
        raise RuntimeError(f"No Level II objects for {site}: {last_error or 'empty listing'}")

    def download(self, bucket: str, key: str) -> bytes:
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
        with httpx.Client(timeout=config.NEXRAD_TIMEOUT, follow_redirects=True, headers={"User-Agent": config.NWS_USER_AGENT}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content

    def analyze_site(self, site_id: str) -> dict[str, Any]:
        site = site_by_id(site_id)
        if not site:
            raise ValueError(f"Unknown NEXRAD site {site_id}")
        cached = self.cache.get(site["id"])
        if cached and _fresh(cached.get("analyzedAt"), config.ROTATION_POLL_SECONDS):
            return cached
        if not self._lock.acquire(blocking=False):
            return self.cache.get(site["id"]) or {
                "ok": False,
                "site": site,
                "error": "rotation analysis already running",
                "signatures": [],
            }
        try:
            return self._analyze_site_locked(site)
        finally:
            self._lock.release()

    def _analyze_site_locked(self, site: dict[str, Any]) -> dict[str, Any]:
        if not HAS_METPY:
            result = {
                "ok": False,
                "site": site,
                "error": "metpy is not installed; rotation decode unavailable",
                "signatures": [],
            }
            self.cache[site["id"]] = result
            return result
        try:
            bucket, key = self.latest_object(site["id"])
            log.info("NEXRAD downloading %s from %s (%s)", key, bucket, site["id"])
            raw = self.download(bucket, key)
            log.info("NEXRAD decoding %s (%.1f MB)", site["id"], len(raw) / 1e6)
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexrad")
            fut = pool.submit(decode_rotation, raw, site)
            try:
                signatures, meta = fut.result(timeout=50)
            except Exception as exc:
                raise TimeoutError(f"Level II decode timed out or failed: {exc}") from exc
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            result = {
                "ok": True,
                "site": site,
                "key": key,
                "bucket": bucket,
                "analyzedAt": datetime.now(timezone.utc).isoformat(),
                "volumeTime": meta.get("volumeTime"),
                "elevation": meta.get("elevation"),
                "signatures": signatures,
            }
            self.cache[site["id"]] = result
            self.last_ok = result["analyzedAt"]
            self.last_site = site["id"]
            self.last_error = None
            log.info(
                "NEXRAD %s: %d signatures from %s",
                site["id"],
                len(signatures),
                key,
            )
            return result
        except Exception as exc:
            log.warning("NEXRAD analyze failed for %s: %s", site["id"], exc)
            self.last_error = str(exc)
            result = {
                "ok": False,
                "site": site,
                "error": str(exc),
                "signatures": [],
                "analyzedAt": datetime.now(timezone.utc).isoformat(),
            }
            return result

    def analyze_near(self, lat: float, lon: float) -> dict[str, Any]:
        site = nearest_nexrad(lat, lon)
        return self.analyze_site(site["id"])

    async def publish(self, result: dict[str, Any]) -> None:
        await hub.broadcast({"type": "rotation", **result})


S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def _list_keys(bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token = None
    url = f"https://{bucket}.s3.amazonaws.com/"
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": config.NWS_USER_AGENT}) as client:
        while True:
            params: dict[str, str] = {"list-type": "2", "prefix": prefix, "max-keys": "500"}
            if token:
                params["continuation-token"] = token
            resp = client.get(url, params=params)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            for node in root.findall("s3:Contents/s3:Key", S3_NS):
                key = node.text or ""
                name = key.rsplit("/", 1)[-1]
                if not key or "_MDM" in name or name.endswith(".txt"):
                    continue
                keys.append(key)
            truncated = (root.findtext("s3:IsTruncated", default="false", namespaces=S3_NS) or "").lower() == "true"
            if not truncated:
                break
            token = root.findtext("s3:NextContinuationToken", namespaces=S3_NS)
            if not token:
                break
            # Keep walking so we land on the newest object in this prefix.
    keys.sort()
    return keys


def _fresh(iso_ts: str | None, ttl: int) -> bool:
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() < ttl


def polar_to_latlon(lat: float, lon: float, az_deg: float, range_km: float) -> tuple[float, float]:
    r = 6371.0
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    brng = math.radians(az_deg)
    d = range_km / r
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _moment_dict(ray: tuple) -> dict:
    if len(ray) > 4 and isinstance(ray[4], dict):
        return ray[4]
    for item in ray:
        if isinstance(item, dict) and (b"VEL" in item or b"REF" in item or "VEL" in item):
            return item
    return {}


def _moment(moments: dict, name: str):
    key = name.encode() if name.encode() in moments else name
    return moments.get(key)


def extract_velocity_sweep(level2: Any) -> tuple[Any, float] | tuple[None, None]:
    best = None
    best_el = 99.0
    for sweep in level2.sweeps:
        if not sweep:
            continue
        hdr = sweep[0][0]
        el = float(getattr(hdr, "el_angle", 99))
        moments = _moment_dict(sweep[0])
        if _moment(moments, "VEL") is None:
            found = False
            step = max(1, len(sweep) // 12)
            for ray in sweep[::step]:
                if _moment(_moment_dict(ray), "VEL") is not None:
                    found = True
                    break
            if not found:
                continue
        if el < best_el:
            best = sweep
            best_el = el
    if best is None:
        return None, None
    return best, best_el


def decode_rotation(raw: bytes, site: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not HAS_METPY:
        return [], {}
    suffix = ".gz" if raw[:2] == b"\x1f\x8b" else ".dat"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        Path(path).write_bytes(raw)
        log.info("NEXRAD Level2File open %s", site["id"])
        level2 = Level2File(path)
        log.info("NEXRAD Level2File parsed %s sweeps=%s", site["id"], len(getattr(level2, "sweeps", []) or []))
        sweep, el = extract_velocity_sweep(level2)
        if sweep is None:
            return [], {"volumeTime": str(getattr(level2, "dt", "")), "elevation": None}

        az = []
        vel_rows = []
        ranges = None
        for ray in sweep:
            hdr = ray[0]
            moments = _moment_dict(ray)
            block = _moment(moments, "VEL")
            if not block:
                continue
            vel_hdr, vel_data = block[0], block[1]
            if ranges is None:
                n_gates = int(getattr(vel_hdr, "num_gates", len(vel_data)))
                width = float(getattr(vel_hdr, "gate_width", 0.25))
                first = float(getattr(vel_hdr, "first_gate", 0.0))
                ranges = (np.arange(n_gates) * width + first).astype(float)
            az.append(float(getattr(hdr, "az_angle", 0.0)))
            row = np.array(vel_data, dtype=float)
            vel_rows.append(row)

        meta = {
            "volumeTime": getattr(level2, "dt", None).isoformat() if getattr(level2, "dt", None) else None,
            "elevation": el,
            "stid": getattr(level2, "stid", site["id"]),
        }
        if not vel_rows or ranges is None:
            return [], meta

        max_g = max(len(r) for r in vel_rows)
        vel = np.full((len(vel_rows), max_g), np.nan, dtype=float)
        for i, row in enumerate(vel_rows):
            vel[i, : len(row)] = row[:max_g]
        if vel.shape[1] != len(ranges):
            ranges = ranges[: vel.shape[1]]

        az = np.array(az, dtype=float)
        order = np.argsort(az)
        vel = vel[order]
        az = az[order]

        signatures = find_mesocyclones(vel, az, ranges, site["lat"], site["lon"])
        return signatures, meta
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def find_mesocyclones(
    vel: Any,
    az_deg: Any,
    ranges_km: Any,
    radar_lat: float,
    radar_lon: float,
) -> list[dict[str, Any]]:
    """Azimuthal shear / gate-to-gate velocity couplet detector."""
    n_rays, n_gates = vel.shape
    g0 = int(np.searchsorted(ranges_km, 8.0))
    g1 = int(min(n_gates, np.searchsorted(ranges_km, 150.0)))
    if g1 - g0 < 4 or n_rays < 20:
        return []

    vel = vel[:, g0:g1:2]
    rng = ranges_km[g0:g1:2]
    az = np.deg2rad(az_deg)
    daz = np.diff(az, append=az[0] + 2 * np.pi)
    daz = (daz + np.pi) % (2 * np.pi) - np.pi
    rolled = np.roll(vel, -1, axis=0)
    dvel = rolled - vel
    arc = np.abs(daz)[:, None] * np.maximum(rng[None, :] * 1000.0, 1.0)
    shear = dvel / arc
    couplet = ((vel < -4) & (rolled > 4)) | (dvel >= 20)
    mask = np.isfinite(dvel) & (dvel >= 12) & (arc >= 250) & (arc <= 12000) & (shear >= 0.006) & couplet
    ii, gg = np.where(mask)
    if ii.size == 0:
        return []
    order = np.argsort(-dvel[ii, gg])
    hits = []
    for idx in order[:400]:
        i = int(ii[idx])
        g = int(gg[idx])
        dv = float(dvel[i, g])
        r_km = float(rng[g])
        a_m = float(arc[i, g])
        kind = "tvs" if (dv >= 25 and a_m < 2500) else "meso"
        lat, lon = polar_to_latlon(radar_lat, radar_lon, float(az_deg[i]), r_km)
        hits.append(
            {
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "rangeKm": round(r_km, 1),
                "azimuth": round(float(az_deg[i]), 1),
                "deltaMs": round(dv, 1),
                "shear": round(float(shear[i, g]), 4),
                "kind": kind,
                "inbound": round(float(vel[i, g]), 1),
                "outbound": round(float(rolled[i, g]), 1),
            }
        )
    return cluster_hits(hits)


def cluster_hits(hits: list[dict[str, Any]], radius_km: float = 6.0) -> list[dict[str, Any]]:
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: -h["deltaMs"])
    clusters: list[list[dict[str, Any]]] = []
    used = [False] * len(hits)
    for i, hit in enumerate(hits):
        if used[i]:
            continue
        group = [hit]
        used[i] = True
        for j in range(i + 1, len(hits)):
            if used[j]:
                continue
            if haversine(hit["lat"], hit["lon"], hits[j]["lat"], hits[j]["lon"]) <= radius_km:
                group.append(hits[j])
                used[j] = True
        clusters.append(group)

    out = []
    for group in clusters:
        peak = max(group, key=lambda h: h["deltaMs"])
        lat = sum(h["lat"] for h in group) / len(group)
        lon = sum(h["lon"] for h in group) / len(group)
        kind = "tvs" if any(h["kind"] == "tvs" for h in group) or peak["deltaMs"] >= 25 else "meso"
        if len(group) < 2 and peak["deltaMs"] < 25:
            continue
        strength = min(1.0, peak["deltaMs"] / 40.0)
        out.append(
            {
                **peak,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "kind": kind,
                "strength": round(strength, 2),
                "samples": len(group),
                "label": "TVS-like" if kind == "tvs" else "Mesocyclone",
            }
        )
        if len(out) >= 24:
            break
    return out


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


nexrad = NexradService()

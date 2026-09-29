"""Live cameras for a searched city.

FAA WeatherCams stills are proxied from the public WeatherCams API.
Every state also gets its official road-camera map plus a webcam map
centered on the coordinates the user searched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import geo

log = logging.getLogger("stormdesk.cams")

FAA_SITES = "https://weathercams.faa.gov/api/sites"
FAA_LAST = "https://weathercams.faa.gov/api/cameras/{camera_id}/images/last/1"
FAA_PAGE = "https://weathercams.faa.gov/cameras/cameraSite/{site_id}/details/camera/{camera_id}"
IMAGE_HOSTS = {"images.wcams-static.faa.gov"}
SITES_TTL = 30 * 60
IMAGE_TTL = 45
MAX_IMAGE = 4_000_000

# Browser-like UA: the WeatherCams API rejects a bare client with 401.
FAA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,image/*;q=0.9,*/*;q=0.8",
    "Referer": "https://weathercams.faa.gov/",
}

# Official traveler-information / road-camera maps. One stable page per state.
PORTALS: dict[str, tuple[str, str]] = {
    "AL": ("ALGO Traffic", "https://algotraffic.com/"),
    "AK": ("Alaska 511", "https://511.alaska.gov/"),
    "AZ": ("AZ 511", "https://az511.gov/"),
    "AR": ("IDrive Arkansas", "https://www.idrivearkansas.com/"),
    "CA": ("Caltrans QuickMap", "https://quickmap.dot.ca.gov/"),
    "CO": ("COtrip", "https://www.cotrip.org/"),
    "CT": ("CT Roads", "https://ctroads.org/"),
    "DE": ("DelDOT map", "https://www.deldot.gov/map/"),
    "DC": ("DDOT", "https://ddot.dc.gov/"),
    "FL": ("FL511", "https://fl511.com/"),
    "GA": ("Georgia 511", "https://511ga.org/"),
    "HI": ("Hawaii DOT", "https://hidot.hawaii.gov/highways/"),
    "ID": ("Idaho 511", "https://511.idaho.gov/"),
    "IL": ("Getting Around Illinois", "https://www.gettingaroundillinois.com/"),
    "IN": ("Indiana 511", "https://511in.org/"),
    "IA": ("Iowa 511", "https://511ia.org/"),
    "KS": ("KanDrive", "https://www.kandrive.org/"),
    "KY": ("GoKY", "https://goky.ky.gov/"),
    "LA": ("511LA", "https://www.511la.org/"),
    "ME": ("New England 511", "https://newengland511.org/"),
    "MD": ("MD 511", "https://www.511md.org/"),
    "MA": ("Mass511", "https://mass511.com/"),
    "MI": ("Mi Drive", "https://mdotjboss.state.mi.us/MiDrive/map"),
    "MN": ("511MN", "https://511mn.org/"),
    "MS": ("MDOT Traffic", "https://www.mdottraffic.com/"),
    "MO": ("Traveler Information", "https://traveler.modot.org/map"),
    "MT": ("MDT traveler map", "https://www.mdt.mt.gov/travinfo/map.aspx"),
    "NE": ("Nebraska 511", "https://511.nebraska.gov/"),
    "NV": ("NV Roads", "https://www.nvroads.com/"),
    "NH": ("New England 511", "https://newengland511.org/"),
    "NJ": ("511NJ", "https://511nj.org/"),
    "NM": ("NM Roads", "https://www.nmroads.com/"),
    "NY": ("511NY", "https://511ny.org/"),
    "NC": ("DriveNC", "https://drivenc.gov/"),
    "ND": ("ND Travel", "https://travel.dot.nd.gov/"),
    "OH": ("OHGO", "https://ohgo.com/"),
    "OK": ("OKTraffic", "https://www.oktraffic.org/"),
    "OR": ("TripCheck", "https://www.tripcheck.com/"),
    "PA": ("511PA", "https://www.511pa.com/"),
    "RI": ("New England 511", "https://newengland511.org/"),
    "SC": ("511SC", "https://www.511sc.org/"),
    "SD": ("SD511", "https://www.sd511.org/"),
    "TN": ("TDOT SmartWay", "https://smartway.tn.gov/traffic"),
    "TX": ("DriveTexas", "https://drivetexas.org/"),
    "UT": ("UDOT Traffic", "https://www.udottraffic.utah.gov/"),
    "VT": ("New England 511", "https://newengland511.org/"),
    "VA": ("Virginia 511", "https://511.vdot.virginia.gov/"),
    "WA": ("WSDOT cameras", "https://wsdot.com/travel/real-time/cameras"),
    "WV": ("WV511", "https://wv511.org/"),
    "WI": ("511WI", "https://511wi.gov/"),
    "WY": ("Wyoming Road Info", "https://www.wyoroad.info/"),
    "PR": ("Puerto Rico DTOP", "https://www.dtop.pr.gov/"),
    "GU": ("Guam DPW", "https://dpw.guam.gov/"),
    "VI": ("USVI DPW", "https://dpw.vi.gov/"),
}

_sites: list[dict[str, Any]] = []
_sites_at = 0.0
_images: dict[int, tuple[float, bytes, str]] = {}
_client: httpx.AsyncClient | None = None
_warm_lock = asyncio.Lock()


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=FAA_HEADERS, timeout=httpx.Timeout(20.0), follow_redirects=True)
    return _client


async def aclose() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def _slim(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for site in payload:
        if not site.get("siteActive"):
            continue
        try:
            lat = float(site["latitude"])
            lon = float(site["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        cams = []
        for cam in site.get("cameras") or []:
            if cam.get("cameraOutOfOrder"):
                continue
            try:
                cid = int(cam["cameraId"])
            except (KeyError, TypeError, ValueError):
                continue
            cams.append(
                {
                    "id": cid,
                    "direction": cam.get("cameraDirection") or "",
                    "name": cam.get("cameraName") or "",
                }
            )
        if not cams:
            continue
        out.append(
            {
                "siteId": int(site["siteId"]),
                "name": site.get("siteName") or "Camera",
                "state": (site.get("state") or "").upper(),
                "lat": lat,
                "lon": lon,
                "cameras": cams,
            }
        )
    return out


async def warm() -> int:
    global _sites, _sites_at
    async with _warm_lock:
        if _sites and time.monotonic() - _sites_at < SITES_TTL:
            return len(_sites)
        http = await client()
        resp = await http.get(FAA_SITES)
        resp.raise_for_status()
        payload = resp.json().get("payload") or []
        _sites = _slim(payload)
        _sites_at = time.monotonic()
        log.info("FAA WeatherCams %s sites", len(_sites))
        return len(_sites)


async def _ensure() -> list[dict[str, Any]]:
    if not _sites or time.monotonic() - _sites_at > SITES_TTL:
        try:
            await warm()
        except Exception:
            log.warning("FAA WeatherCams catalog unavailable", exc_info=True)
    return _sites


def windy_url(lat: float, lon: float) -> str:
    return f"https://www.windy.com/?{lat:.4f},{lon:.4f},11,i:cams"


def portals_for(state: str | None, lat: float | None, lon: float | None, name: str | None) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    st = (state or "").upper()
    portal = PORTALS.get(st)
    if portal:
        links.append({"label": f"{portal[0]} road cameras", "url": portal[1], "kind": "roads"})
    if lat is not None and lon is not None:
        place = name or "this spot"
        links.append({"label": f"Webcams near {place}", "url": windy_url(lat, lon), "kind": "webcams"})
    return links


async def nearest(lat: float, lon: float, limit: int = 8, radius_km: float = 200) -> list[dict[str, Any]]:
    sites = await _ensure()
    ranked: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for site in sites:
        dist = geo.haversine_km(lat, lon, site["lat"], site["lon"])
        if dist > radius_km:
            continue
        for cam in site["cameras"]:
            ranked.append((dist, site, cam))
    ranked.sort(key=lambda item: (item[0], item[2]["direction"]))
    # One still per site keeps a city from filling up with four looks at the same ramp.
    shots = []
    seen_sites: set[int] = set()
    for dist, site, cam in ranked:
        if site["siteId"] in seen_sites:
            continue
        seen_sites.add(site["siteId"])
        shots.append(
            {
                "id": cam["id"],
                "name": site["name"],
                "direction": cam["direction"],
                "state": site["state"],
                "lat": site["lat"],
                "lon": site["lon"],
                "km": round(dist, 1),
                "image": f"/api/cam/faa/{cam['id']}",
                "page": FAA_PAGE.format(site_id=site["siteId"], camera_id=cam["id"]),
                "attribution": "FAA WeatherCams",
            }
        )
        if len(shots) >= limit:
            break
    return shots


async def latest_image(camera_id: int) -> tuple[bytes, str] | None:
    cached = _images.get(camera_id)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1], cached[2]
    http = await client()
    meta = await http.get(FAA_LAST.format(camera_id=camera_id))
    if meta.status_code != 200:
        return None
    payload = (meta.json() or {}).get("payload") or []
    if not payload:
        return None
    uri = payload[0].get("imageUri") or ""
    host = urlparse(uri).hostname or ""
    if host not in IMAGE_HOSTS or not uri.startswith("https://"):
        log.warning("rejected camera image host %s", host)
        return None
    image = await http.get(uri)
    if image.status_code != 200:
        return None
    ctype = (image.headers.get("content-type") or "image/jpeg").split(";")[0].strip().lower()
    if not ctype.startswith("image/") or len(image.content) > MAX_IMAGE or len(image.content) < 32:
        return None
    _images[camera_id] = (now + IMAGE_TTL, image.content, ctype)
    if len(_images) > 400:
        oldest = sorted(_images, key=lambda key: _images[key][0])[:100]
        for key in oldest:
            _images.pop(key, None)
    return image.content, ctype

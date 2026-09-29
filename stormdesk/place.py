"""Weather.gov links, NOAA Weather Radio, and cameras for a map point."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from . import cams, config, geo, nwr, streams

log = logging.getLogger("stormdesk.place")

POINTS_TTL = 6 * 60 * 60
FORECAST_TTL = 15 * 60

_points: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}
_forecasts: dict[str, tuple[float, str]] = {}
_client: httpx.AsyncClient | None = None


async def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": config.NWS_USER_AGENT, "Accept": "application/geo+json"},
            timeout=httpx.Timeout(12.0),
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def _key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 2), round(lon, 2))


def _mapclick(lat: float, lon: float, graphical: bool = False) -> str:
    url = f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}"
    if graphical:
        url += "&FcstType=graphical"
    return url


async def _points_doc(lat: float, lon: float) -> dict[str, Any] | None:
    key = _key(lat, lon)
    cached = _points.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    http = await client()
    resp = await http.get(f"{config.NWS_BASE}/points/{lat:.4f},{lon:.4f}")
    if resp.status_code != 200:
        return None
    doc = resp.json()
    _points[key] = (now + POINTS_TTL, doc)
    return doc


async def _summary(forecast_url: str) -> str:
    cached = _forecasts.get(forecast_url)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    http = await client()
    resp = await http.get(forecast_url)
    if resp.status_code != 200:
        return ""
    periods = ((resp.json().get("properties") or {}).get("periods")) or []
    bits = []
    for period in periods[:2]:
        name = period.get("name") or "Forecast"
        temp = period.get("temperature")
        unit = period.get("temperatureUnit") or ""
        short = period.get("shortForecast") or ""
        wind = " ".join(bit for bit in (period.get("windDirection"), period.get("windSpeed")) if bit)
        line = f"{name}: {temp}°{unit} {short}".strip()
        if wind:
            line += f", wind {wind}"
        bits.append(line)
    text = " · ".join(bits)
    _forecasts[forecast_url] = (now + FORECAST_TTL, text)
    return text


def _point_from_codes(codes: list[str]) -> tuple[float, float, str] | None:
    wanted: set[str] = set()
    for raw in codes:
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 5:
            wanted.add(digits[-5:].zfill(5))
    if not wanted:
        return None
    for county in geo.counties():
        fips = str(county.get("f") or "").zfill(5)
        if fips in wanted:
            return float(county["lat"]), float(county["lon"]), str(county["s"])
    return None


async def weather_block(lat: float | None, lon: float | None) -> dict[str, Any]:
    links: list[dict[str, str]] = []
    summary = ""
    place_name = ""
    if lat is None or lon is None:
        return {"summary": "", "place": "", "links": links}
    links.append({"id": "forecast", "label": "Forecast", "url": _mapclick(lat, lon)})
    links.append({"id": "hourly", "label": "Hourly", "url": _mapclick(lat, lon, graphical=True)})
    try:
        doc = await asyncio.wait_for(_points_doc(lat, lon), timeout=4)
    except TimeoutError:
        doc = None
    except Exception:
        log.warning("NWS points lookup failed", exc_info=True)
        doc = None
    props = (doc or {}).get("properties") or {}
    rel = ((props.get("relativeLocation") or {}).get("properties")) or {}
    city = rel.get("city") or ""
    st = rel.get("state") or ""
    if city and st:
        place_name = f"{city}, {st}"
    wfo = ""
    office = props.get("forecastOffice") or ""
    if isinstance(office, str) and office.rsplit("/", 1)[-1]:
        wfo = office.rstrip("/").rsplit("/", 1)[-1].upper()
    if wfo and len(wfo) <= 4 and wfo.isalpha():
        links.append(
            {
                "id": "discussion",
                "label": "Discussion",
                "url": f"https://forecast.weather.gov/product.php?site={wfo}&issuedby={wfo}&product=AFD",
            }
        )
        links.append({"id": "office", "label": f"NWS {wfo}", "url": f"https://www.weather.gov/{wfo.lower()}/"})
    radar = props.get("radarStation") or ""
    if isinstance(radar, str) and radar:
        links.append(
            {
                "id": "radar",
                "label": f"Radar {radar}",
                "url": f"https://radar.weather.gov/station/{radar}/standard",
            }
        )
    forecast_url = props.get("forecast")
    if isinstance(forecast_url, str) and forecast_url.startswith("https://api.weather.gov/"):
        try:
            summary = await asyncio.wait_for(_summary(forecast_url), timeout=4)
        except TimeoutError:
            summary = ""
        except Exception:
            log.warning("NWS forecast summary failed", exc_info=True)
    return {"summary": summary, "place": place_name, "links": links}


async def describe(
    *,
    lat: float | None,
    lon: float | None,
    state: str | None,
    fips: list[str],
    name: str | None,
    same: list[str],
) -> dict[str, Any]:
    codes = [*(fips or []), *(same or [])]
    if lat is None or lon is None:
        found = _point_from_codes(codes)
        if found:
            lat, lon, state = found[0], found[1], state or found[2]
    try:
        weather = await weather_block(lat, lon)
    except Exception:
        log.warning("weather block failed", exc_info=True)
        weather = {"summary": "", "place": "", "links": []}
    if not state:
        tail = (weather.get("place") or "").rsplit(", ", 1)
        if len(tail) == 2 and len(tail[1]) == 2 and tail[1].isalpha():
            state = tail[1].upper()
    radio = nwr.stations_for(lat=lat, lon=lon, state=state, fips=codes, limit=8)
    await streams.attach(radio)
    shots: list[dict[str, Any]] = []
    if lat is not None and lon is not None:
        try:
            shots = await cams.nearest(lat, lon)
        except Exception:
            log.warning("camera lookup failed", exc_info=True)
    label = (name or "").strip() or weather.get("place") or ""
    link_name = weather.get("place") or label
    if label in {"Map center", "My location"} and weather.get("place"):
        link_name = weather["place"]
    return {
        "ok": True,
        "name": label,
        "state": (state or "").upper(),
        "lat": lat,
        "lon": lon,
        "weather": weather,
        "radio": {
            "program": nwr.PROGRAM,
            "listing": nwr.LISTING.format(state=(state or "US").upper()) if state else nwr.PROGRAM,
            "stations": radio,
        },
        "cameras": {
            "shots": shots,
            "portals": cams.portals_for(state, lat, lon, link_name or None),
        },
    }

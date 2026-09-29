"""Volunteer NOAA Weather Radio streams from noaaweatherradio.org.

NOAA does not publish an internet feed for every transmitter. The streams
here are receivers that volunteers point at a specific repeater and publish
through wxradio.org. A transmitter with no entry simply has no player.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("stormdesk.streams")

CATALOG = "https://noaaweatherradio.org/java/NWR-radios-data.js"
DIRECTORY = "https://noaaweatherradio.org/"
TTL = 60 * 60
LIVE_HOSTS = frozenset(
    {
        "wxradio.org",
        "www.urberg.net",
        "stream.mikev.com",
        "noaaradio.herseyweather.com",
        "broadcast.bismarckweather.net",
        "noaa-manassas-radio.from-va.com",
    }
)

_lock = asyncio.Lock()
_index: dict[str, dict[str, str]] = {}
_fresh_until = 0.0
_client: httpx.AsyncClient | None = None


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": config.NWS_USER_AGENT, "Accept": "application/javascript"},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def _clean(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _live_url(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or host not in LIVE_HOSTS:
        return None
    if not parsed.path or parsed.path == "/":
        return None
    return parsed.geturl()


def _page(row: dict[str, Any]) -> str:
    site = (row.get("wxurl") or "").strip()
    parsed = urlparse(site)
    if parsed.scheme == "https" and parsed.netloc:
        return site
    return DIRECTORY


def _build(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        call = str(row.get("call") or "").upper().strip()
        if not call or call.startswith("NWRORG"):
            continue
        url = _live_url(str(row.get("radiourl") or ""))
        if not url:
            continue
        grouped.setdefault(call, []).append({**row, "_url": url})
    index: dict[str, dict[str, str]] = {}
    for call, rows in grouped.items():
        rows.sort(key=lambda row: (row.get("alt") != "N", "wxradio.org" not in row["_url"]))
        primary = rows[0]
        fallback = next((row["_url"] for row in rows[1:] if row["_url"] != primary["_url"]), "")
        entry = {
            "url": primary["_url"],
            "page": _page(primary),
            "provider": _clean(str(primary.get("who") or ""))[:80],
        }
        if fallback:
            entry["fallback"] = fallback
        index[call] = entry
    return index


async def ensure() -> None:
    global _fresh_until
    now = time.monotonic()
    if _index and _fresh_until > now:
        return
    async with _lock:
        now = time.monotonic()
        if _index and _fresh_until > now:
            return
        http = await _http()
        resp = await http.get(CATALOG)
        resp.raise_for_status()
        text = resp.text.strip()
        if text.startswith("var data ="):
            text = text[len("var data =") :].strip()
        if text.endswith(";"):
            text = text[:-1]
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("NWR stream catalog was not an object")
        built = _build(payload)
        if not built:
            raise ValueError("NWR stream catalog had no live mounts")
        _index.clear()
        _index.update(built)
        _fresh_until = time.monotonic() + TTL
        log.info("NOAA Weather Radio streams %s", len(_index))


async def attach(stations: list[dict[str, Any]]) -> None:
    try:
        await asyncio.wait_for(ensure(), timeout=4)
    except Exception:
        log.warning("NOAA Weather Radio stream catalog unavailable", exc_info=True)
        return
    for station in stations:
        hit = _index.get(str(station.get("call") or "").upper())
        if hit:
            station["stream"] = dict(hit)

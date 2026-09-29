"""NOAA NWS active-alert poller with in-memory cache and dedupe."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config
from .hub import hub

log = logging.getLogger("stormdesk.nws")

SEVERITY_RANK = {"Extreme": 5, "Severe": 4, "Moderate": 3, "Minor": 2, "Unknown": 1}

TORNADO_RE = re.compile(r"tornado|particularly dangerous situation|pds", re.I)
SEVERE_RE = re.compile(r"severe thunder|severe weather|destructive", re.I)
FLOOD_RE = re.compile(r"flash flood|flood|hydrologic", re.I)
WINTER_RE = re.compile(
    r"winter|blizzard|ice storm|snow squall|freezing rain|lake effect|wind chill",
    re.I,
)
TROP_RE = re.compile(r"hurricane|tropical storm|typhoon|storm surge|cyclone", re.I)
FIRE_RE = re.compile(r"red flag|fire weather|wildfire", re.I)
MOTION_RE = re.compile(
    r"moving\s+(north-?east|north-?west|south-?east|south-?west|north|south|east|west|"
    r"nne|nnw|sse|ssw|ene|ese|wne|wnw|ne|nw|se|sw|n|s|e|w)"
    r"(?:ward)?\s+at\s+(\d+)\s*(mph|kt|knots)?",
    re.I,
)

DIR_TO_DEG = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
    "NORTH": 0, "NORTHEAST": 45, "EAST": 90, "SOUTHEAST": 135,
    "SOUTH": 180, "SOUTHWEST": 225, "WEST": 270, "NORTHWEST": 315,
    "NORTH-EAST": 45, "NORTH-WEST": 315, "SOUTH-EAST": 135, "SOUTH-WEST": 225,
}

EVENT_COLORS = {
    "tornado": "#ff2d55",
    "severe": "#ff6b00",
    "flood": "#00e676",
    "winter": "#82b1ff",
    "tropical": "#e040fb",
    "fire": "#ff8a65",
    "other": "#00e5ff",
}


def classify_event(event: str) -> str:
    if TORNADO_RE.search(event):
        return "tornado"
    if TROP_RE.search(event):
        return "tropical"
    if SEVERE_RE.search(event):
        return "severe"
    if FLOOD_RE.search(event):
        return "flood"
    if WINTER_RE.search(event):
        return "winter"
    if FIRE_RE.search(event):
        return "fire"
    return "other"


def parse_motion(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    m = MOTION_RE.search(text)
    if not m:
        return None
    raw_dir = m.group(1).upper().replace("WARD", "")
    deg = DIR_TO_DEG.get(raw_dir)
    if deg is None:
        return None
    speed = int(m.group(2))
    unit = (m.group(3) or "mph").lower()
    mph = speed * 1.15078 if unit.startswith("kt") else float(speed)
    return {"dir": raw_dir, "deg": deg, "mph": round(mph, 1)}


def _states_from_ugc(ugc: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for code in ugc:
        if len(code) >= 2:
            st = code[:2].upper()
            if st not in seen:
                seen.add(st)
                out.append(st)
    return out


def normalize_alert(feature: dict[str, Any]) -> dict[str, Any] | None:
    props = feature.get("properties") or {}
    alert_id = props.get("id") or feature.get("id")
    if not alert_id:
        return None
    event = props.get("event") or "Alert"
    kind = classify_event(event)
    geocode = props.get("geocode") or {}
    ugc = list(geocode.get("UGC") or [])
    same = list(geocode.get("SAME") or [])
    description = props.get("description") or ""
    headline = props.get("headline") or event
    motion = parse_motion(description) or parse_motion(headline)
    return {
        "id": alert_id,
        "event": event,
        "headline": headline,
        "severity": props.get("severity") or "Unknown",
        "urgency": props.get("urgency") or "Unknown",
        "certainty": props.get("certainty") or "Unknown",
        "severityRank": SEVERITY_RANK.get(props.get("severity") or "Unknown", 1),
        "kind": kind,
        "color": EVENT_COLORS[kind],
        "areas": props.get("areaDesc") or "",
        "states": _states_from_ugc(ugc),
        "ugc": ugc,
        "same": same,
        "onset": props.get("onset"),
        "expires": props.get("expires") or props.get("ends"),
        "ends": props.get("ends"),
        "effective": props.get("effective"),
        "sent": props.get("sent"),
        "description": description,
        "instruction": props.get("instruction") or "",
        "sender": props.get("senderName") or "",
        "response": props.get("response") or "",
        "messageType": props.get("messageType") or "Alert",
        "status": props.get("status") or "Actual",
        "parameters": props.get("parameters") or {},
        "motion": motion,
        "geometry": feature.get("geometry"),
        "updated": props.get("sent"),
    }


class AlertStore:
    def __init__(self) -> None:
        self.alerts: dict[str, dict[str, Any]] = {}
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self.last_fetch_count = 0
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": config.NWS_USER_AGENT,
                    "Accept": config.NWS_ACCEPT,
                },
                timeout=httpx.Timeout(config.HTTP_TIMEOUT),
                follow_redirects=True,
            )
        return self._client

    def snapshot(self) -> list[dict[str, Any]]:
        return sorted(
            self.alerts.values(),
            key=lambda a: (-a["severityRank"], a.get("sent") or ""),
        )

    def get(self, alert_id: str) -> dict[str, Any] | None:
        return self.alerts.get(alert_id)

    async def refresh(self) -> dict[str, Any]:
        client = await self.client()
        try:
            resp = await client.get(config.NWS_ALERTS, params={"status": "actual"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("NWS fetch failed: %s", exc)
            return {"ok": False, "error": str(exc)}

        features = payload.get("features") or []
        incoming: dict[str, dict[str, Any]] = {}
        for feat in features:
            alert = normalize_alert(feat)
            if alert:
                incoming[alert["id"]] = alert

        prev_ids = set(self.alerts)
        new_ids = set(incoming)
        added = [incoming[i] for i in new_ids - prev_ids]
        expired_ids = list(prev_ids - new_ids)
        updated: list[dict[str, Any]] = []
        for aid in new_ids & prev_ids:
            old, new = self.alerts[aid], incoming[aid]
            if (
                old.get("updated") != new.get("updated")
                or old.get("expires") != new.get("expires")
                or old.get("headline") != new.get("headline")
                or old.get("geometry") != new.get("geometry")
            ):
                updated.append(new)

        self.alerts = incoming
        self.last_ok = datetime.now(timezone.utc).isoformat()
        self.last_error = None
        self.last_fetch_count = len(incoming)

        if added or updated or expired_ids:
            await hub.broadcast(
                {
                    "type": "alerts",
                    "new": added,
                    "updated": updated,
                    "expired": expired_ids,
                    "count": len(incoming),
                    "fetchedAt": self.last_ok,
                }
            )
            log.info(
                "alerts: %d active, +%d ~%d -%d",
                len(incoming),
                len(added),
                len(updated),
                len(expired_ids),
            )
        return {
            "ok": True,
            "count": len(incoming),
            "added": len(added),
            "updated": len(updated),
            "expired": len(expired_ids),
        }


store = AlertStore()

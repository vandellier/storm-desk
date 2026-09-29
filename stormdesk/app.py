"""FastAPI application: REST, WebSocket, static frontend, background pollers."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import cams, config, geo, place, streams
from .hub import hub
from .nexrad import nexrad
from .nws import store as alerts
from .radar import HASH_RE, PRODUCT_RE, SITE_RE, radar

log = logging.getLogger("stormdesk")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

TASKS: list[asyncio.Task] = []
FOCUS_SITE = {"id": "KTLX"}
FOCUS_EVENT = asyncio.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TASKS.extend(
        [
            asyncio.create_task(_poll_alerts(), name="alerts"),
            asyncio.create_task(_poll_radar(), name="radar"),
            asyncio.create_task(_poll_tracks(), name="tracks"),
            asyncio.create_task(_poll_rotation(), name="rotation"),
            asyncio.create_task(_warm_cameras(), name="cameras"),
            asyncio.create_task(_warm_streams(), name="streams"),
        ]
    )
    log.info("StormDesk started on http://%s:%s", config.HOST, config.PORT)
    try:
        yield
    finally:
        for task in TASKS:
            task.cancel()
        await asyncio.gather(*TASKS, return_exceptions=True)
        if alerts._client:
            await alerts._client.aclose()
        if radar._client:
            await radar._client.aclose()
        await place.aclose()
        await cams.aclose()
        await streams.aclose()


app = FastAPI(title="StormDesk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/api/config")
async def client_config():
    return {
        "cartoKey": config.CARTO_KEY,
        "basemap": "carto" if config.CARTO_KEY else "esri",
    }


@app.get("/api/status")
async def status():
    return {
        "ok": True,
        "name": "StormDesk",
        "time": datetime.now(timezone.utc).isoformat(),
        "clients": hub.count,
        "alerts": {
            "count": len(alerts.alerts),
            "lastOk": alerts.last_ok,
            "lastError": alerts.last_error,
        },
        "radar": {
            "frames": len(radar.frames.get("past") or []),
            "lastOk": radar.last_frames_ok,
            "tracks": len(radar.tracks.get("features") or []),
            "tracksOk": radar.last_tracks_ok,
            "error": radar.last_error,
        },
        "nexrad": {
            "site": nexrad.last_site,
            "lastOk": nexrad.last_ok,
            "lastError": nexrad.last_error,
            "focus": FOCUS_SITE.get("id"),
        },
        "pollSeconds": config.POLL_SECONDS,
    }


@app.get("/api/alerts")
async def list_alerts(
    state: str | None = None,
    ugc: str | None = None,
    kind: str | None = None,
):
    items = alerts.snapshot()
    if state:
        wanted = {s.strip().upper() for s in state.split(",") if s.strip()}
        items = [a for a in items if wanted & set(a.get("states") or [])]
    if ugc:
        wanted_u = {u.strip().upper() for u in ugc.split(",") if u.strip()}
        items = [a for a in items if wanted_u & set(a.get("ugc") or [])]
    if kind:
        wanted_k = {k.strip().lower() for k in kind.split(",") if k.strip()}
        items = [a for a in items if a.get("kind") in wanted_k]
    return {"count": len(items), "fetchedAt": alerts.last_ok, "alerts": items}


@app.get("/api/alerts/{alert_id:path}")
async def get_alert(alert_id: str):
    item = alerts.get(alert_id)
    if not item:
        raise HTTPException(404, "Alert not found")
    return item


@app.get("/api/geo/states")
async def list_states():
    return {"states": geo.states()}


@app.get("/api/geo/counties")
async def list_counties(state: str | None = None, q: str | None = None, limit: int = 200):
    return {"counties": geo.search_counties(state, q, min(limit, 500))}


@app.get("/api/geo/cities")
async def list_cities(state: str | None = None, q: str | None = None, limit: int = 40):
    return {"cities": geo.search_cities(state, q, min(limit, 80))}


def _split_codes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()][:40]


@app.get("/api/place")
async def place_desk(
    lat: float | None = None,
    lon: float | None = None,
    state: str | None = None,
    fips: str | None = None,
    name: str | None = None,
    same: str | None = None,
):
    if lat is not None and not -90 <= lat <= 90:
        raise HTTPException(400, "Invalid latitude")
    if lon is not None and not -180 <= lon <= 180:
        raise HTTPException(400, "Invalid longitude")
    if (lat is None) ^ (lon is None):
        raise HTTPException(400, "Latitude and longitude are both required")
    st = (state or "").strip().upper()
    if st and (len(st) != 2 or not st.isalpha()):
        raise HTTPException(400, "Invalid state")
    return await place.describe(
        lat=lat,
        lon=lon,
        state=st or None,
        fips=_split_codes(fips),
        name=(name or "").strip()[:80] or None,
        same=_split_codes(same),
    )


@app.get("/api/cam/faa/{camera_id}")
async def faa_camera(camera_id: int):
    if camera_id < 1 or camera_id > 10_000_000:
        raise HTTPException(400, "Invalid camera")
    try:
        image = await cams.latest_image(camera_id)
    except Exception as exc:
        log.warning("camera %s failed: %s", camera_id, exc)
        raise HTTPException(502, "Camera image unavailable") from exc
    if not image:
        raise HTTPException(404, "Camera image unavailable")
    body, ctype = image
    return Response(
        content=body,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=40"},
    )


@app.get("/api/nexrad/sites")
async def nexrad_sites():
    return {"sites": geo.NEXRAD_SITES}


@app.get("/api/nexrad/nearest")
async def nexrad_nearest(lat: float, lon: float):
    return geo.nearest_nexrad(lat, lon)


@app.get("/api/nexrad/rotation")
async def nexrad_rotation(
    site: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
):
    if site:
        sid = site.upper()
        if not SITE_RE.match(sid) and not re.match(r"^K[A-Z]{3}$", sid):
            raise HTTPException(400, "Invalid site id")
        FOCUS_SITE["id"] = sid
        result = await asyncio.to_thread(nexrad.analyze_site, sid)
    elif lat is not None and lon is not None:
        near = geo.nearest_nexrad(lat, lon)
        FOCUS_SITE["id"] = near["id"]
        result = await asyncio.to_thread(nexrad.analyze_site, near["id"])
    else:
        result = await asyncio.to_thread(nexrad.analyze_site, FOCUS_SITE["id"])
    return result


@app.get("/api/radar/frames")
async def radar_frames():
    if not radar.frames.get("past"):
        await radar.refresh_frames()
    return radar.frames


@app.get("/api/radar/tracks")
async def radar_tracks():
    return radar.tracks


@app.get("/api/radar/reflectivity/{frame}/{z}/{x}/{y}.png")
async def reflectivity_tile(
    frame: str,
    z: int,
    x: int,
    y: int,
    color: int = Query(6, ge=0, le=12),
    options: str = Query("1_1"),
):
    _check_xyz(z, x, y)
    if z > 7:
        url = radar.iem_mosaic_url(z, x, y, 0)
        key = f"iem-n0q:{z}/{x}/{y}"
    else:
        if not HASH_RE.match(frame):
            raise HTTPException(400, "Invalid frame id")
        opts = options if re.match(r"^[01]_[01]$", options) else "1_1"
        url = radar.rainviewer_tile_url(frame, z, x, y, color, opts)
        key = f"rv:{frame}:{z}/{x}/{y}:{color}:{opts}"
    return await _tile_response(url, key)


@app.get("/api/radar/velocity/{site}/{z}/{x}/{y}.png")
async def velocity_tile(site: str, z: int, x: int, y: int):
    _check_xyz(z, x, y)
    sid = site.upper()
    if not SITE_RE.match(sid):
        raise HTTPException(400, "Invalid site id")
    url = radar.iem_ridge_url(sid, "N0U", z, x, y)
    return await _tile_response(url, f"vel:{sid}:{z}/{x}/{y}")


@app.get("/api/radar/stormrel/{site}/{z}/{x}/{y}.png")
async def storm_relative_tile(site: str, z: int, x: int, y: int):
    _check_xyz(z, x, y)
    sid = site.upper()
    if not SITE_RE.match(sid):
        raise HTTPException(400, "Invalid site id")
    url = radar.iem_ridge_url(sid, "N0S", z, x, y)
    return await _tile_response(url, f"n0s:{sid}:{z}/{x}/{y}")


@app.get("/api/radar/tracks-raster/{site}/{z}/{x}/{y}.png")
async def tracks_raster_tile(site: str, z: int, x: int, y: int):
    _check_xyz(z, x, y)
    sid = site.upper()
    if not SITE_RE.match(sid):
        raise HTTPException(400, "Invalid site id")
    url = radar.iem_ridge_url(sid, "NST", z, x, y)
    return await _tile_response(url, f"nst:{sid}:{z}/{x}/{y}")


@app.get("/api/radar/iem/{site}/{product}/{z}/{x}/{y}.png")
async def iem_tile(site: str, product: str, z: int, x: int, y: int):
    _check_xyz(z, x, y)
    sid = site.upper()
    prod = product.upper()
    if not SITE_RE.match(sid) or not PRODUCT_RE.match(prod):
        raise HTTPException(400, "Invalid site or product")
    url = radar.iem_ridge_url(sid, prod, z, x, y)
    return await _tile_response(url, f"iem:{sid}:{prod}:{z}/{x}/{y}")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        await ws.send_json(
            {
                "type": "hello",
                "alerts": alerts.snapshot(),
                "fetchedAt": alerts.last_ok,
                "frames": radar.frames,
                "tracks": radar.tracks,
                "focusSite": FOCUS_SITE.get("id"),
                "rotation": _cached_rotation(FOCUS_SITE.get("id")),
                "pollSeconds": config.POLL_SECONDS,
            }
        )
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "focus" and msg.get("lat") is not None and msg.get("lon") is not None:
                near = geo.nearest_nexrad(float(msg["lat"]), float(msg["lon"]))
                FOCUS_SITE["id"] = near["id"]
                FOCUS_EVENT.set()
                await ws.send_json({"type": "focus", "site": near})
                cached = _cached_rotation(near["id"])
                if cached:
                    await ws.send_json({"type": "rotation", **cached})
            elif kind == "rotate":
                sid = (msg.get("site") or FOCUS_SITE.get("id") or "KTLX").upper()
                FOCUS_SITE["id"] = sid
                result = await asyncio.to_thread(nexrad.analyze_site, sid)
                await ws.send_json({"type": "rotation", **result})
            elif kind == "ping":
                await ws.send_json({"type": "pong", "t": datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:
        await hub.disconnect(ws)


def _check_xyz(z: int, x: int, y: int) -> None:
    if not (0 <= z <= 12 and x >= 0 and y >= 0):
        raise HTTPException(400, "Invalid tile coordinates")
    max_idx = 2**z
    if x >= max_idx or y >= max_idx:
        raise HTTPException(400, "Tile out of range")


EMPTY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a4944415478da63000000020001e221bc330000000049454e44ae426082"
)


async def _tile_response(url: str, key: str) -> Response:
    try:
        body, ctype = await radar.fetch_tile(url, key)
    except Exception as exc:
        log.debug("tile fail %s: %s", url, exc)
        return Response(
            content=EMPTY_PNG,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=15"},
        )
    return Response(
        content=body,
        media_type=(ctype.split(";")[0] if ctype else "image/png"),
        headers={"Cache-Control": "public, max-age=45"},
    )


async def _warm_cameras() -> None:
    try:
        await cams.warm()
    except Exception:
        log.warning("camera catalog warm failed", exc_info=True)


async def _warm_streams() -> None:
    try:
        await streams.ensure()
    except Exception:
        log.warning("radio stream catalog warm failed", exc_info=True)


async def _poll_alerts() -> None:
    await asyncio.sleep(0.2)
    while True:
        try:
            await alerts.refresh()
        except Exception:
            log.exception("alert poll crashed")
        await asyncio.sleep(config.POLL_SECONDS)


async def _poll_radar() -> None:
    await asyncio.sleep(0.4)
    while True:
        try:
            await radar.refresh_frames()
            await hub.broadcast({"type": "frames", "frames": radar.frames})
        except Exception:
            log.exception("radar poll crashed")
        await asyncio.sleep(max(60, config.POLL_SECONDS))


async def _poll_tracks() -> None:
    await asyncio.sleep(1.0)
    while True:
        try:
            await radar.refresh_tracks()
        except Exception:
            log.exception("track poll crashed")
        await asyncio.sleep(config.TRACK_POLL_SECONDS)


def _cached_rotation(site_id: str | None) -> dict | None:
    if not site_id:
        return None
    cached = nexrad.cache.get(site_id)
    if cached and cached.get("ok"):
        return cached
    return None


async def _poll_rotation() -> None:
    await asyncio.sleep(1.0)
    while True:
        FOCUS_EVENT.clear()
        site = FOCUS_SITE["id"]
        try:
            result = await asyncio.to_thread(nexrad.analyze_site, site)
            if result.get("error") != "rotation analysis already running" and FOCUS_SITE["id"] == site:
                await nexrad.publish(result)
        except Exception:
            log.exception("rotation poll crashed")
        if FOCUS_EVENT.is_set():
            continue
        try:
            await asyncio.wait_for(FOCUS_EVENT.wait(), config.ROTATION_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass



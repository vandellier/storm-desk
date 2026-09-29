# StormDesk

Local personal storm tracker. Python backend, vanilla JS frontend. No Docker.

StormDesk polls the NOAA National Weather Service for active alerts, proxies RainViewer radar mosaics, overlays IEM velocity and storm tracks, and decodes NEXRAD Level II from `s3://noaa-nexrad-level2` to flag rotation signatures.

## Run

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

`run.bat` does the same thing with the venv interpreter. The `python` command on PATH is a separate install and is not used.

Open http://127.0.0.1:8000

## What it does

- Polls `https://api.weather.gov/alerts/active` every 2 minutes, caches and dedupes by alert ID, and pushes new/updated/expired alerts over WebSocket
- Serves a dark Leaflet map with alert polygons, animated storm tracks, and a radar loop
- Proxies RainViewer reflectivity tiles (no API key) and IEM NEXRAD velocity / storm-track tiles so the browser never hits those hosts directly
- Downloads the latest Level II volume for the NEXRAD site nearest the map (NOAA open data on `s3://unidata-nexrad-level2`, same layout as the retired `s3://noaa-nexrad-level2` listing) and extracts Doppler velocity for mesocyclone / TVS-like couplets
- Filters the sidebar by state, county, and city; browser notifications and a sound ping when a warning drops in your area
- Keeps a bottom alert ticker. Clicking an alert opens a reader that stays open until you close it, on desktop, tablet, and phone
- For the area you search, links to weather.gov (forecast, hourly, discussion, office, radar), the NOAA Weather Radio transmitters that cover that county, and live FAA weather cameras plus the state road-camera map and webcams at that spot
- Plays a volunteer stream of a NOAA Weather Radio transmitter when one is published. Most repeaters are radio-only and stay as coverage links

## API

| Path | Purpose |
| --- | --- |
| `GET /api/alerts` | Cached active alerts |
| `GET /api/radar/frames` | RainViewer loop frames |
| `GET /api/radar/reflectivity/{frame}/{z}/{x}/{y}.png` | Proxied mosaic tiles |
| `GET /api/radar/velocity/{site}/{z}/{x}/{y}.png` | Proxied base velocity |
| `GET /api/nexrad/rotation?site=KTLX` | Level II rotation signatures |
| `GET /api/place` | weather.gov links, NOAA Weather Radio, and cameras for a point |
| `GET /api/cam/faa/{id}` | Proxied FAA WeatherCam still |
| `WS /ws` | Live alert, track, and rotation updates |

Optional env: `STORMDESK_HOST`, `STORMDESK_PORT`, `STORMDESK_POLL` (seconds, default 120).

The dark basemap defaults to Esri (no key). CARTO Dark Matter looks nicer but watermarks tiles unless you set a free key from https://carto.com/basemaps/apikey — then put it in `.env`:

```
STORMDESK_CARTO_KEY=your_key_here
```

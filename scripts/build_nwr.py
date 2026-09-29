"""Download NOAA's NWR county-coverage table and write static/data/nwr.json.

Source: https://www.weather.gov/source/nwr/JS/CCL.js
Official listing: https://www.weather.gov/dsb/counties
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "data" / "nwr.json"
URL = "https://www.weather.gov/source/nwr/JS/CCL.js"
FIELDS = (
    "ST",
    "STATE",
    "COUNTY",
    "SAME",
    "SITENAME",
    "SITELOC",
    "SITESTATE",
    "FREQ",
    "CALLSIGN",
    "LAT",
    "LON",
    "PWR",
    "STATUS",
    "WFO",
    "REMARKS",
)
ROW_RE = re.compile(r"(" + "|".join(FIELDS) + r")\[(\d+)\] = \"((?:\\.|[^\"\\])*)\";")


def _unescape(value: str) -> str:
    return (
        value.replace(r"\"", '"')
        .replace(r"\/", "/")
        .replace(r"\n", " ")
        .replace(r"\t", " ")
        .replace(r"\\", "\\")
    )


def main() -> None:
    req = urllib.request.Request(URL, headers={"User-Agent": "StormDesk/1.0 (nwr dataset refresh)"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        text = resp.read().decode("utf-8", "replace")
        updated = resp.headers.get("Last-Modified") or ""
    cols: dict[str, dict[int, str]] = {name: {} for name in FIELDS}
    for match in ROW_RE.finditer(text):
        cols[match.group(1)][int(match.group(2))] = _unescape(match.group(3))
    count = max((max(col) for col in cols.values() if col), default=-1) + 1
    stations: dict[str, dict] = {}
    for i in range(count):
        call = (cols["CALLSIGN"].get(i) or "").strip().upper()
        if not call:
            continue
        try:
            lat = round(float(cols["LAT"].get(i) or 0), 5)
            lon = round(float(cols["LON"].get(i) or 0), 5)
        except ValueError:
            continue
        try:
            watts = int(float(cols["PWR"].get(i) or 0))
        except ValueError:
            watts = 0
        same = (cols["SAME"].get(i) or "").strip()
        county = (cols["COUNTY"].get(i) or "").strip()
        state = (cols["ST"].get(i) or "").strip().upper()
        key = call
        existing = stations.get(key)
        if existing and abs(existing["lat"] - lat) > 0.2:
            key = f"{call}@{lat:.2f}"
        row = stations.get(key)
        if row is None:
            wfo = (cols["WFO"].get(i) or "").split("|")[0].strip()
            row = {
                "call": call,
                "mhz": (cols["FREQ"].get(i) or "").strip(),
                "name": (cols["SITENAME"].get(i) or call).strip(),
                "location": (cols["SITELOC"].get(i) or "").strip(),
                "state": (cols["SITESTATE"].get(i) or state).strip().upper(),
                "lat": lat,
                "lon": lon,
                "watts": watts,
                "status": (cols["STATUS"].get(i) or "UNKNOWN").strip().upper(),
                "wfo": wfo,
                "same": [],
                "counties": [],
                "remarks": (cols["REMARKS"].get(i) or "").strip(),
            }
            stations[key] = row
        if same and same not in row["same"]:
            row["same"].append(same)
        label = f"{county}, {state}" if county and state else county
        if label and label not in row["counties"]:
            row["counties"].append(label)
    payload = {
        "source": URL,
        "listing": "https://www.weather.gov/dsb/counties",
        "updated": updated,
        "stations": list(stations.values()),
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"rows {count} stations {len(payload['stations'])} bytes {OUT.stat().st_size} -> {OUT}")


if __name__ == "__main__":
    main()

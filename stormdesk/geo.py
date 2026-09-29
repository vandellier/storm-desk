"""States, counties, cities, and NEXRAD site lookup."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config

# WSR-88D sites (CONUS + AK/HI/PR/GU). Lat/lon in decimal degrees.
# Source: NOAA WSR-88D location table (DMS converted).
NEXRAD_SITES: list[dict[str, Any]] = [
    {"id": "KABR", "name": "Aberdeen", "state": "SD", "lat": 45.4558, "lon": -98.4131},
    {"id": "KABX", "name": "Albuquerque", "state": "NM", "lat": 35.1497, "lon": -106.8239},
    {"id": "KAKQ", "name": "Norfolk/Richmond", "state": "VA", "lat": 36.9839, "lon": -77.0072},
    {"id": "KAMA", "name": "Amarillo", "state": "TX", "lat": 35.2333, "lon": -101.7092},
    {"id": "KAMX", "name": "Miami", "state": "FL", "lat": 25.6111, "lon": -80.4128},
    {"id": "KAPX", "name": "Gaylord", "state": "MI", "lat": 44.9072, "lon": -84.7197},
    {"id": "KARX", "name": "La Crosse", "state": "WI", "lat": 43.8228, "lon": -91.1911},
    {"id": "KATX", "name": "Seattle", "state": "WA", "lat": 48.1944, "lon": -122.4958},
    {"id": "KBBX", "name": "Beale AFB", "state": "CA", "lat": 39.4961, "lon": -121.6317},
    {"id": "KBGM", "name": "Binghamton", "state": "NY", "lat": 42.1997, "lon": -75.9847},
    {"id": "KBHX", "name": "Eureka", "state": "CA", "lat": 40.4983, "lon": -124.2919},
    {"id": "KBIS", "name": "Bismarck", "state": "ND", "lat": 46.7708, "lon": -100.7606},
    {"id": "KBLX", "name": "Billings", "state": "MT", "lat": 45.8539, "lon": -108.6067},
    {"id": "KBMX", "name": "Birmingham", "state": "AL", "lat": 33.1722, "lon": -86.7699},
    {"id": "KBOX", "name": "Boston", "state": "MA", "lat": 41.9558, "lon": -71.1369},
    {"id": "KBRO", "name": "Brownsville", "state": "TX", "lat": 25.9161, "lon": -97.4189},
    {"id": "KBUF", "name": "Buffalo", "state": "NY", "lat": 42.9489, "lon": -78.7367},
    {"id": "KBYX", "name": "Key West", "state": "FL", "lat": 24.5975, "lon": -81.7031},
    {"id": "KCAE", "name": "Columbia", "state": "SC", "lat": 33.9486, "lon": -81.1183},
    {"id": "KCBW", "name": "Houlton", "state": "ME", "lat": 46.0394, "lon": -67.8067},
    {"id": "KCBX", "name": "Boise", "state": "ID", "lat": 43.4908, "lon": -116.2356},
    {"id": "KCCX", "name": "State College", "state": "PA", "lat": 40.9231, "lon": -78.0036},
    {"id": "KCLE", "name": "Cleveland", "state": "OH", "lat": 41.4131, "lon": -81.8597},
    {"id": "KCLX", "name": "Charleston", "state": "SC", "lat": 32.6556, "lon": -81.0420},
    {"id": "KCRP", "name": "Corpus Christi", "state": "TX", "lat": 27.7842, "lon": -97.5111},
    {"id": "KCXX", "name": "Burlington", "state": "VT", "lat": 44.5111, "lon": -73.1669},
    {"id": "KCYS", "name": "Cheyenne", "state": "WY", "lat": 41.1519, "lon": -104.8061},
    {"id": "KDAX", "name": "Sacramento", "state": "CA", "lat": 38.5011, "lon": -121.6778},
    {"id": "KDDC", "name": "Dodge City", "state": "KS", "lat": 37.7608, "lon": -99.9686},
    {"id": "KDFX", "name": "Laughlin AFB", "state": "TX", "lat": 29.2728, "lon": -100.2806},
    {"id": "KDGX", "name": "Jackson", "state": "MS", "lat": 32.2800, "lon": -89.9844},
    {"id": "KDIX", "name": "Philadelphia", "state": "PA", "lat": 39.9469, "lon": -74.4108},
    {"id": "KDLH", "name": "Duluth", "state": "MN", "lat": 46.8369, "lon": -92.2097},
    {"id": "KDMX", "name": "Des Moines", "state": "IA", "lat": 41.7314, "lon": -93.7228},
    {"id": "KDOX", "name": "Dover AFB", "state": "DE", "lat": 38.8256, "lon": -75.4400},
    {"id": "KDTX", "name": "Detroit", "state": "MI", "lat": 42.6997, "lon": -83.4717},
    {"id": "KDVN", "name": "Davenport", "state": "IA", "lat": 41.6117, "lon": -90.5808},
    {"id": "KDYX", "name": "Dyess AFB", "state": "TX", "lat": 32.5383, "lon": -99.2542},
    {"id": "KEAX", "name": "Kansas City", "state": "MO", "lat": 38.8103, "lon": -94.2644},
    {"id": "KEMX", "name": "Tucson", "state": "AZ", "lat": 31.8936, "lon": -110.6303},
    {"id": "KENX", "name": "Albany", "state": "NY", "lat": 42.5864, "lon": -74.0639},
    {"id": "KEOX", "name": "Fort Rucker", "state": "AL", "lat": 31.4606, "lon": -85.4594},
    {"id": "KEPZ", "name": "El Paso", "state": "TX", "lat": 31.8731, "lon": -106.6981},
    {"id": "KESX", "name": "Las Vegas", "state": "NV", "lat": 35.7011, "lon": -114.8914},
    {"id": "KEVX", "name": "Eglin AFB", "state": "FL", "lat": 30.5644, "lon": -85.9214},
    {"id": "KEWX", "name": "Austin/San Antonio", "state": "TX", "lat": 29.7039, "lon": -98.0283},
    {"id": "KEYX", "name": "Edwards AFB", "state": "CA", "lat": 35.0978, "lon": -117.5608},
    {"id": "KFCX", "name": "Roanoke", "state": "VA", "lat": 37.0244, "lon": -80.2739},
    {"id": "KFDR", "name": "Altus AFB", "state": "OK", "lat": 34.3622, "lon": -98.9764},
    {"id": "KFDX", "name": "Cannon AFB", "state": "NM", "lat": 34.6353, "lon": -103.6300},
    {"id": "KFFC", "name": "Atlanta", "state": "GA", "lat": 33.3636, "lon": -84.5658},
    {"id": "KFSD", "name": "Sioux Falls", "state": "SD", "lat": 43.5878, "lon": -96.7294},
    {"id": "KFSX", "name": "Flagstaff", "state": "AZ", "lat": 34.5744, "lon": -111.1983},
    {"id": "KFTG", "name": "Denver", "state": "CO", "lat": 39.7867, "lon": -104.5458},
    {"id": "KFWS", "name": "Dallas/Fort Worth", "state": "TX", "lat": 32.5731, "lon": -97.3031},
    {"id": "KGGW", "name": "Glasgow", "state": "MT", "lat": 48.2064, "lon": -106.6250},
    {"id": "KGJX", "name": "Grand Junction", "state": "CO", "lat": 39.0622, "lon": -108.2139},
    {"id": "KGLD", "name": "Goodland", "state": "KS", "lat": 39.3664, "lon": -101.7006},
    {"id": "KGRB", "name": "Green Bay", "state": "WI", "lat": 44.4983, "lon": -88.1114},
    {"id": "KGRK", "name": "Fort Hood", "state": "TX", "lat": 30.7219, "lon": -97.3831},
    {"id": "KGRR", "name": "Grand Rapids", "state": "MI", "lat": 42.8939, "lon": -85.5447},
    {"id": "KGSP", "name": "Greer", "state": "SC", "lat": 34.8833, "lon": -82.2200},
    {"id": "KGWX", "name": "Columbus AFB", "state": "MS", "lat": 33.8967, "lon": -88.3289},
    {"id": "KGYX", "name": "Portland", "state": "ME", "lat": 43.8914, "lon": -70.2567},
    {"id": "KHDX", "name": "Holloman AFB", "state": "NM", "lat": 33.0764, "lon": -106.1228},
    {"id": "KHGX", "name": "Houston", "state": "TX", "lat": 29.4719, "lon": -95.0792},
    {"id": "KHNX", "name": "San Joaquin Valley", "state": "CA", "lat": 36.3142, "lon": -119.6322},
    {"id": "KHPX", "name": "Fort Campbell", "state": "KY", "lat": 36.7367, "lon": -87.2850},
    {"id": "KHTX", "name": "Huntsville", "state": "AL", "lat": 34.9306, "lon": -86.0833},
    {"id": "KICT", "name": "Wichita", "state": "KS", "lat": 37.6547, "lon": -97.4428},
    {"id": "KICX", "name": "Cedar City", "state": "UT", "lat": 37.5908, "lon": -112.8622},
    {"id": "KILN", "name": "Cincinnati", "state": "OH", "lat": 39.4203, "lon": -83.8217},
    {"id": "KILX", "name": "Lincoln", "state": "IL", "lat": 40.1506, "lon": -89.3369},
    {"id": "KIND", "name": "Indianapolis", "state": "IN", "lat": 39.7075, "lon": -86.2803},
    {"id": "KINX", "name": "Tulsa", "state": "OK", "lat": 36.1750, "lon": -95.5647},
    {"id": "KIWA", "name": "Phoenix", "state": "AZ", "lat": 33.2892, "lon": -111.6700},
    {"id": "KIWX", "name": "Fort Wayne", "state": "IN", "lat": 41.3589, "lon": -85.7000},
    {"id": "KJAX", "name": "Jacksonville", "state": "FL", "lat": 30.4847, "lon": -81.7019},
    {"id": "KJGX", "name": "Robins AFB", "state": "GA", "lat": 32.6750, "lon": -83.3511},
    {"id": "KJKL", "name": "Jackson", "state": "KY", "lat": 37.5908, "lon": -83.3131},
    {"id": "KLBB", "name": "Lubbock", "state": "TX", "lat": 33.6539, "lon": -101.8142},
    {"id": "KLCH", "name": "Lake Charles", "state": "LA", "lat": 30.1253, "lon": -93.2158},
    {"id": "KLGX", "name": "Langley Hill", "state": "WA", "lat": 47.1169, "lon": -124.1067},
    {"id": "KLIX", "name": "New Orleans", "state": "LA", "lat": 30.3367, "lon": -89.8256},
    {"id": "KLNX", "name": "North Platte", "state": "NE", "lat": 41.9578, "lon": -100.5764},
    {"id": "KLOT", "name": "Chicago", "state": "IL", "lat": 41.6047, "lon": -88.0847},
    {"id": "KLRX", "name": "Elko", "state": "NV", "lat": 40.7397, "lon": -116.8028},
    {"id": "KLSX", "name": "St. Louis", "state": "MO", "lat": 38.6989, "lon": -90.6828},
    {"id": "KLTX", "name": "Wilmington", "state": "NC", "lat": 33.9894, "lon": -78.4290},
    {"id": "KLVX", "name": "Louisville", "state": "KY", "lat": 37.9753, "lon": -85.9439},
    {"id": "KLWX", "name": "Sterling", "state": "VA", "lat": 38.9753, "lon": -77.4778},
    {"id": "KLZK", "name": "Little Rock", "state": "AR", "lat": 34.8367, "lon": -92.2622},
    {"id": "KMAF", "name": "Midland/Odessa", "state": "TX", "lat": 31.9433, "lon": -102.1892},
    {"id": "KMAX", "name": "Medford", "state": "OR", "lat": 42.0811, "lon": -122.7172},
    {"id": "KMBX", "name": "Minot AFB", "state": "ND", "lat": 48.3925, "lon": -100.8650},
    {"id": "KMHX", "name": "Morehead City", "state": "NC", "lat": 34.7761, "lon": -76.8761},
    {"id": "KMKX", "name": "Milwaukee", "state": "WI", "lat": 42.9678, "lon": -88.5506},
    {"id": "KMLB", "name": "Melbourne", "state": "FL", "lat": 28.1133, "lon": -80.6542},
    {"id": "KMOB", "name": "Mobile", "state": "AL", "lat": 30.6794, "lon": -88.2397},
    {"id": "KMPX", "name": "Minneapolis", "state": "MN", "lat": 44.8489, "lon": -93.5656},
    {"id": "KMQT", "name": "Marquette", "state": "MI", "lat": 46.5311, "lon": -87.5483},
    {"id": "KMRX", "name": "Knoxville", "state": "TN", "lat": 36.1686, "lon": -83.4017},
    {"id": "KMSX", "name": "Missoula", "state": "MT", "lat": 47.0411, "lon": -113.9861},
    {"id": "KMTX", "name": "Salt Lake City", "state": "UT", "lat": 41.2628, "lon": -112.4478},
    {"id": "KMUX", "name": "San Francisco", "state": "CA", "lat": 37.1550, "lon": -121.8983},
    {"id": "KMVX", "name": "Grand Forks", "state": "ND", "lat": 47.5278, "lon": -97.3256},
    {"id": "KMXX", "name": "Maxwell AFB", "state": "AL", "lat": 32.5367, "lon": -85.7897},
    {"id": "KNKX", "name": "San Diego", "state": "CA", "lat": 32.9189, "lon": -117.0419},
    {"id": "KNQA", "name": "Memphis", "state": "TN", "lat": 35.3447, "lon": -89.8733},
    {"id": "KOAX", "name": "Omaha", "state": "NE", "lat": 41.3203, "lon": -96.3667},
    {"id": "KOHX", "name": "Nashville", "state": "TN", "lat": 36.2472, "lon": -86.5625},
    {"id": "KOKX", "name": "New York City", "state": "NY", "lat": 40.8656, "lon": -72.8639},
    {"id": "KOTX", "name": "Spokane", "state": "WA", "lat": 47.6803, "lon": -117.6267},
    {"id": "KPAH", "name": "Paducah", "state": "KY", "lat": 37.0683, "lon": -88.7719},
    {"id": "KPBZ", "name": "Pittsburgh", "state": "PA", "lat": 40.5317, "lon": -80.2183},
    {"id": "KPDT", "name": "Pendleton", "state": "OR", "lat": 45.6906, "lon": -118.8528},
    {"id": "KPOE", "name": "Fort Polk", "state": "LA", "lat": 31.1556, "lon": -92.9758},
    {"id": "KPUX", "name": "Pueblo", "state": "CO", "lat": 38.4594, "lon": -104.1814},
    {"id": "KRAX", "name": "Raleigh/Durham", "state": "NC", "lat": 35.6656, "lon": -78.4897},
    {"id": "KRGX", "name": "Reno", "state": "NV", "lat": 39.7553, "lon": -119.4622},
    {"id": "KRIW", "name": "Riverton", "state": "WY", "lat": 43.0661, "lon": -108.4772},
    {"id": "KRLX", "name": "Charleston", "state": "WV", "lat": 38.3111, "lon": -81.7231},
    {"id": "KRTX", "name": "Portland", "state": "OR", "lat": 45.7147, "lon": -122.9656},
    {"id": "KSFX", "name": "Pocatello", "state": "ID", "lat": 43.1058, "lon": -112.6861},
    {"id": "KSGF", "name": "Springfield", "state": "MO", "lat": 37.2353, "lon": -93.4006},
    {"id": "KSHV", "name": "Shreveport", "state": "LA", "lat": 32.4508, "lon": -93.8414},
    {"id": "KSJT", "name": "San Angelo", "state": "TX", "lat": 31.3714, "lon": -100.4925},
    {"id": "KSOX", "name": "Santa Ana Mtns", "state": "CA", "lat": 33.8178, "lon": -117.6358},
    {"id": "KSRX", "name": "Fort Smith", "state": "AR", "lat": 35.2906, "lon": -94.3617},
    {"id": "KTBW", "name": "Tampa", "state": "FL", "lat": 27.7056, "lon": -82.4017},
    {"id": "KTFX", "name": "Great Falls", "state": "MT", "lat": 47.4597, "lon": -111.3853},
    {"id": "KTLH", "name": "Tallahassee", "state": "FL", "lat": 30.3975, "lon": -84.3289},
    {"id": "KTLX", "name": "Oklahoma City", "state": "OK", "lat": 35.3331, "lon": -97.2778},
    {"id": "KTWX", "name": "Topeka", "state": "KS", "lat": 38.9969, "lon": -96.2325},
    {"id": "KTYX", "name": "Montague", "state": "NY", "lat": 43.7558, "lon": -75.6800},
    {"id": "KUDX", "name": "Rapid City", "state": "SD", "lat": 44.1250, "lon": -102.8297},
    {"id": "KUEX", "name": "Hastings", "state": "NE", "lat": 40.3208, "lon": -98.4419},
    {"id": "KVAX", "name": "Moody AFB", "state": "GA", "lat": 30.8903, "lon": -83.0017},
    {"id": "KVBX", "name": "Vandenberg AFB", "state": "CA", "lat": 34.8381, "lon": -120.3975},
    {"id": "KVNX", "name": "Vance AFB", "state": "OK", "lat": 36.7408, "lon": -98.1278},
    {"id": "KVTX", "name": "Los Angeles", "state": "CA", "lat": 34.4117, "lon": -119.1797},
    {"id": "KVWX", "name": "Evansville", "state": "IN", "lat": 38.2603, "lon": -87.7244},
    {"id": "KYUX", "name": "Yuma", "state": "AZ", "lat": 32.4953, "lon": -114.6567},
    {"id": "PAHG", "name": "Anchorage", "state": "AK", "lat": 60.7258, "lon": -151.3514},
    {"id": "PABC", "name": "Bethel", "state": "AK", "lat": 60.7928, "lon": -161.8742},
    {"id": "PACG", "name": "Sitka", "state": "AK", "lat": 56.8528, "lon": -135.5292},
    {"id": "PAIH", "name": "Middleton Island", "state": "AK", "lat": 59.4619, "lon": -146.3011},
    {"id": "PAKC", "name": "King Salmon", "state": "AK", "lat": 58.6794, "lon": -156.6294},
    {"id": "PAPD", "name": "Fairbanks", "state": "AK", "lat": 65.0350, "lon": -147.5017},
    {"id": "PHKI", "name": "South Kauai", "state": "HI", "lat": 21.8942, "lon": -159.5522},
    {"id": "PHKM", "name": "Kamuela", "state": "HI", "lat": 20.1256, "lon": -155.7778},
    {"id": "PHMO", "name": "Molokai", "state": "HI", "lat": 21.1328, "lon": -157.1800},
    {"id": "PHWA", "name": "Hawaii", "state": "HI", "lat": 19.0950, "lon": -155.5689},
    {"id": "TJUA", "name": "San Juan", "state": "PR", "lat": 18.1156, "lon": -66.0781},
    {"id": "PGUA", "name": "Andersen AFB", "state": "GU", "lat": 13.4544, "lon": 144.8083},
]


def _load_json(name: str) -> Any:
    path: Path = config.DATA_DIR / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def states() -> list[dict[str, Any]]:
    return _load_json("states.json")


@lru_cache(maxsize=1)
def counties() -> list[dict[str, Any]]:
    return _load_json("counties.json")


@lru_cache(maxsize=1)
def cities() -> list[dict[str, Any]]:
    return _load_json("cities.json")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_nexrad(lat: float, lon: float) -> dict[str, Any]:
    best = NEXRAD_SITES[0]
    best_d = 1e18
    for site in NEXRAD_SITES:
        d = haversine_km(lat, lon, site["lat"], site["lon"])
        if d < best_d:
            best, best_d = site, d
    return {**best, "distanceKm": round(best_d, 1)}


def site_by_id(site_id: str) -> dict[str, Any] | None:
    sid = site_id.upper()
    for site in NEXRAD_SITES:
        if site["id"] == sid:
            return site
    return None


def search_counties(state: str | None, q: str | None, limit: int = 80) -> list[dict[str, Any]]:
    qn = (q or "").strip().lower()
    st = (state or "").strip().upper()
    out = []
    for c in counties():
        if st and c["s"] != st:
            continue
        if qn and qn not in c["n"].lower() and qn not in c["u"].lower():
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def search_cities(state: str | None, q: str | None, limit: int = 40) -> list[dict[str, Any]]:
    qn = (q or "").strip().lower()
    st = (state or "").strip().upper()
    out = []
    for c in cities():
        if st and c["s"] != st:
            continue
        if qn and qn not in c["n"].lower():
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out

"""
School Bus Routes — geocoding client.
AMap primary (free 高德 key), OSM/Nominatim fallback.
Mock mode when no key: returns random location in HK.
"""
import csv, json, math, os, random, sys, time
import urllib.parse, urllib.request
from pathlib import Path
from config import (
    AMAP_KEY, AMAP_GEOCODE_URL, OSM_GEOCODE_URL,
    GEOCODE_DELAY_SECS, HK_CENTRE_LAT, HK_CENTRE_LNG,
    DISTRICT_CENTRES, MOCK_JITTER_DEG, DATA_DIR,
)

def haversine(lat1, lng1, lat2, lng2):
    """Distance in metres between two (lat, lng) points."""
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lng2 - lng1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _mock_location(district=""):
    """Mock geocode: scatter a point around the district centre (or HK centre)."""
    centre = DISTRICT_CENTRES.get(district, (HK_CENTRE_LAT, HK_CENTRE_LNG))
    return (
        round(centre[0] + random.uniform(-MOCK_JITTER_DEG, MOCK_JITTER_DEG), 6),
        round(centre[1] + random.uniform(-MOCK_JITTER_DEG, MOCK_JITTER_DEG), 6),
    )


def _geocode_amap(address, district=""):
    params = urllib.parse.urlencode({
        "key": AMAP_KEY, "address": address, "city": district or "香港",
    })
    url = f"{AMAP_GEOCODE_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "1" and data.get("geocodes"):
            loc = data["geocodes"][0].get("location", "")
            lng, lat = [float(x) for x in loc.split(",")]
            return {"lat": lat, "lng": lng, "source": "amap"}
    except Exception:
        pass
    return None


def _geocode_osm(address):
    q = urllib.parse.urlencode({"q": address + " Hong Kong", "format": "json", "limit": 1})
    url = f"{OSM_GEOCODE_URL}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "SchoolBusPlanner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data:
            return {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"]), "source": "osm"}
    except Exception:
        pass
    return None


def _in_hk(lat, lng):
    """True if coordinates fall inside Hong Kong's approximate bounding box.
    AMap sometimes geocodes HK addresses to mainland-China locations."""
    lat_min, lat_max, lng_min, lng_max = (22.14, 22.56, 113.82, 114.45)
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


_manual_cache = None
def _load_manual_locations():
    """Load manual_locations.csv override table, keyed by exact address."""
    global _manual_cache
    if _manual_cache is None:
        _manual_cache = {}
        path = DATA_DIR / "manual_locations.csv"
        if path.exists():
            with open(path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    _manual_cache[row["address"].strip()] = (row["lat"], row["lng"])
    return _manual_cache


_cache = None
def _load_cache():
    """Persistent geocode cache — makes runs deterministic (AMap HK is
    non-deterministic run-to-run, so first good result is locked in)."""
    global _cache
    if _cache is None:
        _cache = {}
        path = DATA_DIR / "geocode_cache.csv"
        if path.exists():
            with open(path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    _cache[row["address"].strip()] = (row["lat"], row["lng"])
    return _cache


def _save_cache():
    if not _cache:
        return
    path = DATA_DIR / "geocode_cache.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["address", "lat", "lng"])
        for addr, (lat, lng) in sorted(_cache.items()):
            w.writerow([addr, lat, lng])


def geocode_one(address, district=""):
    """Geocode a single address. Returns dict with lat, lng, source."""
    # 1) Manual override table — curated corrections, highest priority
    manual = _load_manual_locations()
    if address in manual:
        lat, lng = manual[address]
        return {"lat": float(lat), "lng": float(lng), "source": "manual"}

    # 2) Persistent cache — a previously-verified good result
    cache = _load_cache()
    if address in cache:
        lat, lng = cache[address]
        return {"lat": float(lat), "lng": float(lng), "source": "cache"}

    if AMAP_KEY:
        # 3) AMap, validated to be within HK
        result = _geocode_amap(address, district)
        if result and _in_hk(result["lat"], result["lng"]):
            _cache[address] = (result["lat"], result["lng"])
            _save_cache()
            return result
        time.sleep(GEOCODE_DELAY_SECS)
        # 4) OSM fallback, validated to be within HK
        osm = _geocode_osm(address)
        if osm and _in_hk(osm["lat"], osm["lng"]):
            _cache[address] = (osm["lat"], osm["lng"])
            _save_cache()
            return osm
    # 5) Mock fallback — fast, district-aware; only used when all else fails
    lat, lng = _mock_location(district)
    return {"lat": lat, "lng": lng, "source": "mock"}


def geocode_students(students_csv, output_csv=None):
    """Read students CSV, geocode pickup + (optional) dropoff addresses."""
    rows = list(csv.DictReader(open(students_csv, encoding="utf-8-sig")))
    for i, row in enumerate(rows):
        # Pickup address
        result = geocode_one(row["address"], row.get("district", ""))
        row["lat"]        = result["lat"]
        row["lng"]        = result["lng"]
        row["geocode_source"] = result["source"]
        src = result["source"]
        tag  = "✓" if src in ("amap", "osm", "manual", "cache") else "?"
        print(f"  [{i+1}/{len(rows)}] {row['student_id']} {tag} → {src}", end="")

        # Dropoff address (may differ from pickup). Blank/same -> reuse pickup coords.
        drop = row.get("dropoff_address", "").strip()
        if drop and drop != row.get("address", "").strip():
            dr = geocode_one(drop, row.get("district", ""))
            row["dropoff_lat"]    = dr["lat"]
            row["dropoff_lng"]    = dr["lng"]
            row["dropoff_source"] = dr["source"]
            print(f"  drop@({dr['source']})")
        else:
            row["dropoff_lat"]    = row["lat"]
            row["dropoff_lng"]    = row["lng"]
            row["dropoff_source"] = row["geocode_source"]
            print("  drop=same")
        if src not in ("mock", "manual", "cache") and AMAP_KEY:
            time.sleep(GEOCODE_DELAY_SECS)

    out = output_csv or str(students_csv).replace(".csv", "_geocoded.csv")
    fieldnames = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    matched = sum(1 for r in rows if r["geocode_source"] in ("amap", "osm", "manual", "cache"))
    print(f"\nGeocoded: {matched}/{len(rows)} matched ({matched/len(rows)*100:.0f}%)")
    unmatched = [r for r in rows if r["geocode_source"] not in ("amap", "osm", "manual", "cache")]
    if unmatched:
        path = str(DATA_DIR / "unmatched_addresses.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(unmatched[0].keys()))
            w.writeheader()
            w.writerows(unmatched)
        print(f"Unmatched addresses written to {path}")
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python geocode.py <students_csv>")
        sys.exit(1)
    geocode_students(sys.argv[1])

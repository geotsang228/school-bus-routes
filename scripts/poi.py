"""
School Bus Routes — safe pick-up/drop-off spot finder.
Snaps a cluster centroid to the nearest safe boarding point
(bus stop / 巴士站, coach station / 長途客運站) via AMap POI search.
"""
import json
import sys
import urllib.parse
import urllib.request

from config import (
    AMAP_KEY, AMAP_POI_URL, POI_SAFE_KEYWORDS, POI_SEARCH_RADIUS,
)


def _poi_around(lat, lng, keywords, radius=POI_SEARCH_RADIUS, offset=15):
    """AMap POI search around a point. Returns list of poi dicts or []."""
    if not AMAP_KEY:
        return []
    params = urllib.parse.urlencode({
        "key": AMAP_KEY,
        "location": f"{lng},{lat}",
        "keywords": keywords,
        "radius": radius,
        "offset": offset,
        "extensions": "base",
    })
    url = f"{AMAP_POI_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "1":
            return data.get("pois", [])
    except Exception:
        pass
    return []


def find_safe_stop(lat, lng, radius=POI_SEARCH_RADIUS):
    """
    Find the nearest safe pick-up spot within `radius` metres.
    Priority: bus/coach stop > estate/building entrance > any safe POI.
    Excludes unsafe types (highways, tunnels).
    Returns dict {"name","address","lat","lng","distance"} or None.
    """
    if not AMAP_KEY:
        return None

    results = _poi_around(lat, lng, "|".join(POI_SAFE_KEYWORDS), radius)
    if not results:
        return None

    UNSAFE = ("高速", "隧道", "快速路", "桥梁")

    def _accept(poi):
        ptype = poi.get("type", "")
        if any(u in ptype for u in UNSAFE):
            return False
        return True

    # Priority 1: transit stops (bus/coach)
    TRANSIT = ("公交车站", "长途汽车站", "长途客运站", "汽车客运站", "巴士", "大巴")
    best = None
    for poi in results:
        if not _accept(poi): continue
        ptype = poi.get("type", "")
        loc = poi.get("location", ""); dist = poi.get("distance")
        if not loc or dist is None: continue
        try:
            lng_s, lat_s = loc.split(","); dist_f = float(dist)
        except (ValueError, AttributeError): continue
        if dist_f > radius: continue
        is_transit = any(m in ptype for m in TRANSIT)
        if is_transit:
            if best is None or dist_f < best["distance"]:
                best = {"name": poi.get("name",""), "address": poi.get("address",""),
                        "lat": float(lat_s), "lng": float(lng_s), "distance": round(dist_f,1)}
    if best:
        return best

    # Priority 2: any safe POI (building entrance, estate, landmark)
    for poi in results:
        if not _accept(poi): continue
        loc = poi.get("location", ""); dist = poi.get("distance")
        if not loc or dist is None: continue
        try:
            lng_s, lat_s = loc.split(","); dist_f = float(dist)
        except (ValueError, AttributeError): continue
        if dist_f > radius: continue
        return {"name": poi.get("name",""), "address": poi.get("address",""),
                "lat": float(lat_s), "lng": float(lng_s), "distance": round(dist_f,1)}
    return None


def describe_location(lat, lng):
    """
    Short recognisable description for a stop via AMap reverse geocoding,
    e.g. the precise address of the point. Cached in data/stop_desc_cache.json.
    """
    info = _stop_info(lat, lng)
    return info.get("desc", "")


def stop_street(lat, lng):
    """Nearest street/road name for a stop (from regeo). Cached."""
    info = _stop_info(lat, lng)
    return info.get("street", "")


def stop_landmark(lat, lng):
    """A recognisable nearby spot (7-11, McDonald's, MTR, shop…) for the
    '近 …' part of a stop name. Cached. Returns "" if none found."""
    info = _stop_info(lat, lng)
    return info.get("landmark", "")


def _stop_info(lat, lng):
    """Reverse-geocode once, cache {desc, street, landmark}."""
    import time
    from pathlib import Path
    from config import DATA_DIR

    cache_path = DATA_DIR / "stop_desc_cache.json"
    try:
        cache = json.load(open(cache_path, encoding="utf-8")) if cache_path.exists() else {}
    except Exception:
        cache = {}
    key = f"{round(float(lat), 5)},{round(float(lng), 5)}"
    if key in cache:
        val = cache[key]
        if isinstance(val, str):          # very old format: plain desc
            return {"desc": val, "street": "", "landmark": ""}
        return {"desc": val.get("desc", ""), "street": val.get("street", ""),
                "landmark": val.get("landmark", "")}

    info = {"desc": "", "street": "", "landmark": ""}
    if AMAP_KEY:
        params = urllib.parse.urlencode({
            "key": AMAP_KEY, "location": f"{lng},{lat}", "radius": 300, "extensions": "all",
        })
        url = f"{'https://restapi.amap.com/v3/geocode/regeo'}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            regeo = data.get("regeocode", {})
            addr = regeo.get("formatted_address", "")
            for pre in ("香港特别行政区", "香港特別行政區", "中国", "香港"):
                if addr.startswith(pre):
                    addr = addr[len(pre):]
            pois = regeo.get("pois", [])
            # Prefer an easy-to-recognise landmark (7-11, 麥當勞, KFC, bank, MTR…)
            RECOG = ("7-11", "7 eleven", "便利店", "麥當", "麦当", "kfc", "肯德基",
                     "銀行", "廣場", "商場", "超市", "街市", "藥房", "咖啡",
                     "starbucks", "星巴克", "港鐵", "地鐵", "站", "圖書館", "郵局",
                     "中學", "小學", "公園", "總站")
            landmark = ""
            for p in pois:
                pname = p.get("name", "")
                if any(k in pname for k in RECOG):
                    landmark = pname
                    break
            if not landmark and pois:
                landmark = pois[0].get("name", "")
            info["landmark"] = landmark or ""
            info["desc"] = addr.strip()
            roads = regeo.get("roads", [])
            if roads:
                info["street"] = roads[0].get("name", "")
            if not info["street"]:
                sn = regeo.get("addressComponent", {}).get("streetNumber", {}).get("street", "")
                info["street"] = sn or ""
        except Exception:
            info = {"desc": "", "street": "", "landmark": ""}
        time.sleep(0.15)

    cache[key] = info
    try:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return info


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python poi.py <lat> <lng>")
        sys.exit(1)
    spot = find_safe_stop(float(sys.argv[1]), float(sys.argv[2]))
    if spot:
        print(f"Nearest safe spot: {spot['name']} ({spot['address']})")
        print(f"  {spot['distance']}m away at ({spot['lat']}, {spot['lng']})")
    else:
        print("No safe spot found within radius")

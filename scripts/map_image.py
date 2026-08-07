"""
School Bus Routes — AMap static map builder.
Fetches a real map image with the highlighted route polyline and
numbered stop markers. Returns base64 PNG for embedding in the PDF.

Requires the 靜態地圖 (static map) service enabled on the AMap key.
"""
import base64
import json
import sys
import time
import urllib.parse
import urllib.request

from config import AMAP_KEY, AMAP_STATICMAP_URL

MAP_SIZE = "850*750"
MAX_POLYLINE_POINTS = 5000  # pass the FULL driving polyline so the route follows roads exactly

# Simplified -> Traditional Chinese for map labels (AMap POI names are simplified).
try:
    from opencc import OpenCC
    _cc = OpenCC("s2t")
    def _tc(s):
        return _cc.convert(s) if s else s
except ImportError:
    def _tc(s):
        return s

_last_call = [0.0]
def _pace():
    """Min delay between static-map API calls — free-tier AMap throttles bursts
    (error 10021) after a few rapid calls."""
    wait = 1.0 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def _short_stop_name(name):
    """Trim a POI name to a short recognisable label for the map.
    AMap labels break on spaces and over-length content."""
    if not name:
        return ""
    import re
    name = re.sub(r"[（(].*?[)）]", "", name)          # strip (公交站) etc.
    for suffix in ("巴士總站", "巴士总站", "總站", "总站", "公共汽车站", "公交车站", "巴士站"):
        name = name.replace(suffix, "")
    name = name.replace(" ", "").replace("　", "")
    name = re.sub(r"[,|:，、;]", "", name)              # AMap label separators
    if not name or "Cluster" in name:
        return ""                                      # no real landmark
    return _tc(name[:8])


def _label_safe(s):
    """Sanitise a name for use inside an AMap static-map `labels` value.
    Commas, pipes and colons are field separators in that format, so a stop
    name containing them would corrupt the whole request (AMap rejects it)."""
    import re
    s = _tc(s or "")
    return re.sub(r"[,|:，、;]", "", s).strip()


def _simplify_polyline(polyline, max_points=MAX_POLYLINE_POINTS):
    """Downsample a 'lng,lat;lng,lat' polyline by even stride."""
    pts = polyline.split(";")
    if len(pts) <= max_points:
        return polyline
    stride = len(pts) / max_points
    kept = [pts[int(i * stride)] for i in range(max_points)]
    if kept[-1] != pts[-1]:
        kept.append(pts[-1])
    return ";".join(kept)


def _polyline_points(polyline):
    """Parse 'lng,lat;lng,lat' -> list of (lng, lat) floats."""
    pts = []
    for seg in polyline.split(";"):
        if "," not in seg:
            continue
        lng, lat = seg.split(",")
        try:
            pts.append((float(lng), float(lat)))
        except ValueError:
            pass
    return pts


def _mercator_y(lat):
    """Web-mercator normalized Y (0 at top, 1 at bottom) for a latitude."""
    import math
    lat = math.radians(lat)
    return 0.5 - math.log(math.tan(math.pi / 4 + lat / 2)) / (2 * math.pi)


def _compute_fit(points_lnglat, size=MAP_SIZE, pad=0.10, max_zoom=14):
    """
    Compute location (center) + zoom so the whole route fits inside the
    image with padding. Matches AMap JS setFitView behavior:
    - All markers, route, school included in bounds
    - 80 px blank space on each side (mapped to ~10% pad for 850×750)
    - Maximum zoom 14 to prevent over-zooming
    """
    import math
    lngs = [p[0] for p in points_lnglat]
    lats = [p[1] for p in points_lnglat]
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)

    dlng, dlat = max_lng - min_lng, max_lat - min_lat
    if dlng == 0 and dlat == 0:
        dlng = dlat = 0.003
    min_lng -= dlng * pad; max_lng += dlng * pad
    min_lat -= dlat * pad; max_lat += dlat * pad

    xmin, xmax = min_lng / 360.0, max_lng / 360.0
    ymax, ymin = _mercator_y(min_lat), _mercator_y(max_lat)
    W, H = (int(x) for x in size.split("*"))

    zx = math.log2(W / max((xmax - xmin) * 256, 1e-9))
    zy = math.log2(H / max((ymax - ymin) * 256, 1e-9))
    zoom = int(math.floor(min(zx, zy)))
    zoom = max(1, min(max_zoom, zoom))  # cap at maxZoom

    center_lng = (min_lng + max_lng) / 2
    center_lat = (min_lat + max_lat) / 2
    return center_lat, center_lng, zoom


MAX_URL_LEN = 12000  # AMap static map rejects URLs past ~16 KB (error 20000)


def _fit_paths(polyline, budget_chars):
    """Downsample a polyline so the encoded path fits within budget_chars of
    URL. AMap rejects static-map URLs past a length limit (error 20000), and
    a full multi-stop route polyline can be tens of KB, so the path is
    decimated (by even stride) to whatever point count fits."""
    prefix = "5,0x0000FF,1,,:"  # weight,color,transparency + two empty fields
    pts = polyline.split(";")
    max_pts = max(1, int((budget_chars - len(prefix)) / 21))
    if len(pts) <= max_pts:
        return prefix + polyline
    stride = len(pts) / max_pts
    kept = [pts[int(i * stride)] for i in range(max_pts)]
    if kept[-1] != pts[-1]:
        kept.append(pts[-1])
    return prefix + ";".join(kept)


def build_static_map_url(stops, school_pt, polyline, size=MAP_SIZE):
    """
    Build the AMap static map URL (v3/staticmap).

    markersStyle = size,color,label  (label: [0-9], [A-Z], or single Chinese char)
    labelsStyle  = content,font,bold,fontSize,fontColor,background
    pathsStyle    = weight,color,transparency

    stops: list of {"lat","lng","name"} in route order (numbered + named on the map).
    school_pt: (lat, lng) for the destination marker.
    polyline: "lng,lat;lng,lat;..." route geometry (GCJ-02).
    """
    FIRST_COLOR  = "0xFF0000"  # red for first stop (start point)
    STOP_COLOR   = "0xffa500"  # orange for regular stops (visible on map)
    SCHOOL_COLOR = "0xFF0000"  # red for school

    markers = []
    labels = []
    for i, stop in enumerate(stops, 1):
        is_first = (i == 1)
        color = FIRST_COLOR if is_first else STOP_COLOR
        num = str(i) if i <= 9 else chr(ord("A") + (i - 10))
        markers.append(f"large,{color},{num}:{stop['lng']},{stop['lat']}")

        # Stop name as map label (left of marker) — cleaned + capped
        sname = _short_stop_name(stop.get("name", ""))[:8]
        if sname:
            bg = "0xFF0000" if is_first else "0xffa500"
            lab = f"{sname},0,1,16,0xFFFFFF,{bg}:{float(stop['lng'])-0.0025},{stop['lat']}"
            labels.append(lab)

    markers.append(f"large,{SCHOOL_COLOR},校:{school_pt[1]},{school_pt[0]}")
    labels.append(f"SCHOOL,0,1,16,0xFFFFFF,0xFF0000:{school_pt[1]+0.0025},{school_pt[0]}")

    # AMap caps markers at 10 — keep the school + nearest stops if exceeded
    if len(markers) > 10:
        markers = markers[:9] + [markers[-1]]

    params = {
        "key": AMAP_KEY,
        "size": size,
        "scale": 2,          # hi-res for print
        "markers": "|".join(markers),
        "labels": "|".join(labels),
    }

    # Explicit fit: compute center + zoom from the full route bounds so the
    # whole route (stops + school + polyline) is visible with margin.
    points = [(float(s["lng"]), float(s["lat"])) for s in stops] + [(float(school_pt[1]), float(school_pt[0]))]
    if polyline:
        points += _polyline_points(polyline)
    try:
        clat, clng, zoom = _compute_fit(points, size)
        params["location"] = f"{clng},{clat}"
        # scale=2 bumps the effective zoom by 1 (per AMap doc) → subtract 1 so
        # the rendered area matches the computed fit (otherwise edges get cut off).
        params["zoom"] = max(1, zoom - 1)
    except Exception:
        pass  # fall back to AMap auto-fit

    # Fit the polyline so the final URL stays under AMap's length limit.
    if polyline:
        base = urllib.parse.urlencode({k: v for k, v in params.items() if k != "paths"})
        params["paths"] = _fit_paths(polyline, MAX_URL_LEN - len(base) - 5)
    url = f"{AMAP_STATICMAP_URL}?{urllib.parse.urlencode(params)}"
    # Still over the limit (e.g. many stop-name labels)? Drop the labels and
    # refit the path to the freed-up URL budget.
    if len(url) > MAX_URL_LEN and "labels" in params:
        params.pop("labels")
        base = urllib.parse.urlencode({k: v for k, v in params.items() if k != "paths"})
        if polyline:
            params["paths"] = _fit_paths(polyline, MAX_URL_LEN - len(base) - 5)
        url = f"{AMAP_STATICMAP_URL}?{urllib.parse.urlencode(params)}"
    return url


def fetch_static_map(url, timeout=15):
    """Fetch the static map PNG. Returns bytes or None.
    Patient retry: free-tier AMap throttles bursts (10021) with a cooldown
    that can last ~a minute, so back off far enough to ride it out."""
    _pace()
    for wait in (1, 2, 4, 8, 15, 30, 60):
        try:
            req = urllib.request.Request(url, headers={"Referer": "https://www.amap.com"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
            if data[:8] == b"\x89PNG\r\n\x1a\n" or "image" in ctype:
                return data
        except Exception:
            pass
        time.sleep(wait)
    return None


def static_map_base64(stops, school_pt, polyline, size=MAP_SIZE):
    """Return a data-URI (base64 PNG) for embedding, or None if unavailable."""
    url = build_static_map_url(stops, school_pt, polyline, size)
    data = fetch_static_map(url)
    if not data:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def student_area_map_base64(stops, polyline, size=MAP_SIZE):
    """
    Map focused on the student stops only (excludes the school and the long
    school-bound leg) — used when the school is far from the student area.
    Returns a base64 data-URI or None.
    """
    import base64 as _b64
    markers, labels = [], []
    for i, stop in enumerate(stops, 1):
        is_first = (i == 1)
        color = "0xFF0000" if is_first else "0xffa500"
        num = str(i) if i <= 9 else chr(ord("A") + (i - 10))
        markers.append(f"large,{color},{num}:{stop['lng']},{stop['lat']}")
        sname = _short_stop_name(stop.get("name", ""))[:8]
        if sname:
            bg = "0xFF0000" if is_first else "0xffa500"
            labels.append(f"{sname},0,1,16,0xFFFFFF,{bg}:{float(stop['lng'])-0.0025},{stop['lat']}")
    params = {
        "key": AMAP_KEY, "size": size, "scale": 2,
        "markers": "|".join(markers), "labels": "|".join(labels),
    }
    if polyline:
        params["paths"] = f"5,0x0000FF,1,,:{_simplify_polyline(polyline)}"
    # Fit ONLY to the student stops (not the school, not the school leg)
    points = [(float(s["lng"]), float(s["lat"])) for s in stops]
    try:
        clat, clng, zoom = _compute_fit(points, size)
        params["location"] = f"{clng},{clat}"
        params["zoom"] = max(1, zoom - 1)
    except Exception:
        pass
    url = f"{AMAP_STATICMAP_URL}?{urllib.parse.urlencode(params)}"
    data = fetch_static_map(url)
    if not data:
        return None
    return "data:image/png;base64," + _b64.b64encode(data).decode("ascii")


def point_map_base64(pt, name="", size="360*260"):
    """
    Small single-marker map of one location (e.g. a stop's exact position),
    zoomed to street level so a driver can find it. Returns a base64 PNG
    data-URI or None.
    pt: (lat, lng).
    """
    import base64 as _b64
    params = {
        "key": AMAP_KEY,
        "size": size,
        "scale": 2,
        "markers": f"large,0xffa500,起:{pt[1]},{pt[0]}",
    }
    sname = _short_stop_name(name)[:8]
    if sname:
        params["labels"] = f"{sname},0,1,16,0xFFFFFF,0xffa500:{pt[1]-0.003},{pt[0]}"
    try:
        clat, clng, zoom = _compute_fit([(pt[1], pt[0])], size, max_zoom=17)
        params["location"] = f"{clng},{clat}"
        params["zoom"] = max(1, zoom - 1)  # scale=2 bumps effective zoom by 1
    except Exception:
        pass
    url = f"{AMAP_STATICMAP_URL}?{urllib.parse.urlencode(params)}"
    data = fetch_static_map(url)
    if not data:
        return None
    return "data:image/png;base64," + _b64.b64encode(data).decode("ascii")


def leg_map_base64(origin, dest, polyline, origin_name="", dest_name="", size="460*300"):
    """
    Small static map for one leg (origin → dest) showing the leg's real road
    route, with a detailed labelled marker at point 1 (origin) and point 2 (dest).
    Returns a base64 data-URI for embedding, or None.
    origin/dest: (lat, lng).
    """
    import base64 as _b64
    if not polyline:
        return None
    simple = _simplify_polyline(polyline)
    markers = (
        f"large,0xffa500,起:{origin[1]},{origin[0]}"
        f"|large,0xFF0000,終:{dest[1]},{dest[0]}"
    )
    labels = []
    oname = _label_safe(origin_name)[:10]
    dname = _label_safe(dest_name)[:10]
    if oname:
        labels.append(f"{oname},0,1,16,0xFFFFFF,0xffa500:{origin[1]-0.003},{origin[0]}")
    if dname:
        labels.append(f"{dname},0,1,16,0xFFFFFF,0xFF0000:{dest[1]-0.003},{dest[0]}")
    params = {
        "key": AMAP_KEY,
        "size": size,
        "scale": 2,
        "markers": markers,
        "labels": "|".join(labels),
        "paths": f"5,0x0000FF,1,,:{simple}",
    }
    pts = [(origin[1], origin[0]), (dest[1], dest[0])] + _polyline_points(polyline)
    try:
        clat, clng, zoom = _compute_fit(pts, size)
        params["location"] = f"{clng},{clat}"
        params["zoom"] = max(1, zoom - 1)  # scale=2 bumps effective zoom by 1
    except Exception:
        pass
    url = f"{AMAP_STATICMAP_URL}?{urllib.parse.urlencode(params)}"
    data = fetch_static_map(url)
    if not data:
        return None
    return "data:image/png;base64," + _b64.b64encode(data).decode("ascii")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python map_image.py <route_polylines.json>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    key = list(data.keys())[0]
    poly = data[key].get("fastest", "") or data[key].get("tollfree", "")
    print(f"key={key}, polyline pts={len(poly.split(';')) if poly else 0}")
    img = static_map_base64([{"lat": 22.365, "lng": 114.186}], (22.382, 114.197), poly)
    print("static map:", "OK (%d bytes)" % (len(img)) if img else "FAILED (permission not enabled?)")

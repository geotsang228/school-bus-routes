"""
School Bus Routes — AMap driving-directions client.
Returns real travel time, distance, tolls and route polyline
for both the fastest and toll-free variants of a route.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

from config import AMAP_KEY, AMAP_DRIVING_URL, DRIVING_FASTEST, DRIVING_SHORTEST, DRIVING_TOLLFREE

MAX_WAYPOINTS = 15  # AMap cap (16 total points incl. origin+destination)
CALL_DELAY_SECS = 0.4  # free-tier QPS guard — avoid throttling bursts


def _format_pt(lat, lng):
    """AMap expects lng,lat."""
    return f"{lng},{lat}"


def _select_path(paths, strategy):
    """Pick the path that best matches the strategy. AMap returns several
    alternative paths and paths[0] is not necessarily the best for the chosen
    objective, so choose explicitly: fastest = min duration, shortest = min
    distance, toll-free = min tolls then min duration."""
    def _num(p, key):
        try:
            return float(p.get(key, 0) or 0)
        except (TypeError, ValueError):
            return float("inf")
    if strategy == DRIVING_SHORTEST:
        return min(paths, key=lambda p: _num(p, "distance"))
    if strategy == DRIVING_TOLLFREE:
        return min(paths, key=lambda p: (_num(p, "tolls"), _num(p, "duration")))
    return min(paths, key=lambda p: _num(p, "duration"))


def drive_route(origin, waypoints, destination, strategy=DRIVING_FASTEST):
    """
    Call AMap driving directions for origin -> waypoints -> destination.

    origin/destination: (lat, lng) tuples.
    waypoints: list of (lat, lng) tuples (order matters).

    Returns dict: {distance_m, duration_s, tolls, polyline} or None on failure.
    """
    if not AMAP_KEY:
        return None

    waypoints = list(waypoints)[:MAX_WAYPOINTS]
    params = {
        "key": AMAP_KEY,
        "origin": _format_pt(*origin),
        "destination": _format_pt(*destination),
        "strategy": strategy,
        "extensions": "all",  # returns the full polyline
    }
    if waypoints:
        params["waypoints"] = ";".join(_format_pt(*w) for w in waypoints)

    url = f"{AMAP_DRIVING_URL}?{urllib.parse.urlencode(params)}"

    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
            if data.get("status") == "1":
                paths = data.get("route", {}).get("paths", [])
                if paths:
                    p = _select_path(paths, strategy)
                    # Full polyline: reconstructed from per-step polylines
                    steps = p.get("steps", [])
                    polyline = ";".join(s.get("polyline", "") for s in steps)
                    instructions = [
                        {
                            "instruction": s.get("instruction", ""),
                            "road": s.get("road", ""),
                            "distance_m": int(s.get("distance", 0)),
                            "duration_s": int(s.get("duration", 0)),
                        }
                        for s in steps if s.get("instruction")
                    ]
                    return {
                        "distance_m": int(p.get("distance", 0)),
                        "duration_s": int(p.get("duration", 0)),
                        "tolls": float(p.get("tolls", 0)),
                        "polyline": polyline,
                        "steps": instructions,
                    }
            # status != 1 (rate-limited / no route) — retry
        except Exception:
            pass
        time.sleep(CALL_DELAY_SECS * (attempt + 2))
    return None


def drive_all(origin, waypoints, destination):
    """Return {fastest, shortest, tollfree} result dicts for the same ordered
    waypoints (each may be None). ~0.4 s between calls to dodge the free-tier
    burst throttle."""
    out = {}
    for label, strat in (("fastest", DRIVING_FASTEST),
                         ("shortest", DRIVING_SHORTEST),
                         ("tollfree", DRIVING_TOLLFREE)):
        out[label] = drive_route(origin, waypoints, destination, strat)
        time.sleep(CALL_DELAY_SECS)
    return out


def road_matrix(points):
    """
    Real road driving distance/time between every pair of points, using
    AMap /v3/distance with type=1 (driving, considers traffic).

    points: list of (lat, lng). Returns (dist_m, dur_s) as N×N matrices
    where [i][j] = from point i to point j. diagonal = 0.
    """
    from config import AMAP_DISTANCE_URL
    n = len(points)
    dist = [[0] * n for _ in range(n)]
    dur = [[0] * n for _ in range(n)]
    if n <= 1 or not AMAP_KEY:
        return dist, dur

    for j in range(n):
        idx = [i for i in range(n) if i != j]  # origins (in order)
        origins = "|".join(f"{points[i][1]},{points[i][0]}" for i in idx)
        params = {
            "key": AMAP_KEY,
            "origins": origins,
            "destination": f"{points[j][1]},{points[j][0]}",
            "type": 1,  # driving navigation distance (considers traffic)
        }
        url = f"{AMAP_DISTANCE_URL}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
            if data.get("status") == "1":
                for r in data.get("results", []):
                    oid = int(r.get("origin_id", "0")) - 1  # 1-indexed over origins
                    if 0 <= oid < len(idx):
                        dist[idx[oid]][j] = int(r.get("distance", 0))
                        dur[idx[oid]][j] = int(r.get("duration", 0))
        except Exception:
            pass
        time.sleep(CALL_DELAY_SECS)
    return dist, dur


def format_duration(seconds):
    if seconds is None:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python driving.py <origin_lat> <origin_lng> <dest_lat> <dest_lng>")
        sys.exit(1)
    o = (float(sys.argv[1]), float(sys.argv[2]))
    d = (float(sys.argv[3]), float(sys.argv[4]))
    f, t = drive_variants(o, [], d)
    for label, r in (("fastest", f), ("toll-free", t)):
        if r:
            print(f"{label}: {r['distance_m']}m, {format_duration(r['duration_s'])}, tolls=¥{r['tolls']}, polyline pts={len(r['polyline'].split(';'))}")
        else:
            print(f"{label}: failed")

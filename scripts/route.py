
"""
School Bus Routes — VRP solver (OR-Tools CVRPTW).
Assigns stops to bus routes respecting capacity and school start times.
"""
import csv, math
from collections import defaultdict
from pathlib import Path
from config import (
    DEFAULT_BUS_CAPACITY, DEFAULT_DISMISSAL_TIME, SERVICE_TIME_PER_STOP,
    ARRIVAL_BUFFER_MINS, SOLVER_TIME_LIMIT_SECS, DATA_DIR,
)

def _haversine(lat1, lng1, lat2, lng2):
    R = 6_371_000
    lat1, lng1, lat2, lng2 = float(lat1), float(lng1), float(lat2), float(lng2)
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a  = math.sin(df/2)**2 + math.cos(f1)*math.cos(f2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _build_cost_matrix(stops, school_lat, school_lng):
    school_lat, school_lng = float(school_lat), float(school_lng)
    coords = [(school_lat, school_lng)] + [(float(s["lat"]), float(s["lng"])) for s in stops]
    n = len(coords)
    matrix = [[0]*n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                matrix[i][j] = int(_haversine(*coords[i], *coords[j]))
    return matrix


def _build_demand(stops):
    return [0] + [int(s["students"]) for s in stops]


def _assign_times(route, anchor_mins, trip):
    """Set a time on each stop.
    trip="am": pickup times, back-calculated so the bus arrives at school by anchor.
    trip="pm": dropoff times, calculated forward from the school departure (anchor).
    """
    if trip == "pm":
        t = anchor_mins
        for stop in route:
            stop["pickup_time"] = f"{int(t // 60):02d}:{int(t % 60):02d}"
            t += SERVICE_TIME_PER_STOP + 2
    else:
        arrival = anchor_mins - SERVICE_TIME_PER_STOP
        for stop in reversed(route):
            stop["pickup_time"] = f"{int(arrival // 60):02d}:{int(arrival % 60):02d}"
            arrival -= SERVICE_TIME_PER_STOP + 2
    for i, stop in enumerate(route):
        stop["route_order"] = i + 1


def _tsp_order(n_stops, school_idx, trip, dist, start_stop=None):
    """
    Local TSP ordering (no external solver needed). AMap's driving API does NOT
    optimise waypoint order, so we arrange the stops ourselves:
      - Build a round trip from school (nearest-neighbour) visiting every stop
      - 2-opt that loop to minimise total travel
      - Cut the loop at the school so the bus starts/ends there
    dist: (n_stops+1)x(n_stops+1) minutes matrix, index 0 = school, 1..n = stops.
    AM: pick-up order = the stops of the loop in reverse (ends at school); if a
        driver-specified `start_stop` is given, that stop becomes the first pick-up.
    PM: drop-off order = the stops of the loop forward (starts at school).
    Returns the stop visit order as a list of stop indices (1..n).
    """
    N = n_stops + 1
    stop_idx = list(range(1, N))

    def _path_len(path):
        return sum(dist[path[i]][path[i + 1]] for i in range(len(path) - 1))

    def _nn(start, end, must_visit):
        visited = {start, end}
        path = [start]
        cur = start
        remaining = [i for i in must_visit if i not in visited]
        while remaining:
            nxt = min(remaining, key=lambda i: dist[cur][i])
            path.append(nxt)
            visited.add(nxt)
            cur = nxt
            remaining = [i for i in must_visit if i not in visited]
        path.append(end)
        return path

    def _two_opt(path):
        improved = True
        while improved:
            improved = False
            for i in range(1, len(path) - 2):
                for j in range(i + 1, len(path) - 1):
                    if j == i + 1:
                        continue
                    cur = dist[path[i - 1]][path[i]] + dist[path[j]][path[j + 1]]
                    new = dist[path[i - 1]][path[j]] + dist[path[i]][path[j + 1]]
                    if new < cur - 1e-9:
                        path[i:j + 1] = reversed(path[i:j + 1])
                        improved = True
        return path

    # Round trip: school -> every stop (NN) -> school, then 2-opt the loop
    # with the school pinned at both ends.
    path = _nn(school_idx, school_idx, stop_idx)
    path = _two_opt(path)
    middle = [i for i in path if i != school_idx]
    if trip == "am":
        if start_stop is not None and start_stop in middle:
            # Rotate so the driver-specified stop is the FIRST pick-up
            # (AM order is reversed(middle), so put start_stop at the end).
            k = middle.index(start_stop)
            middle = middle[k + 1:] + middle[:k + 1]
        return list(reversed(middle))
    return middle


def solve_vrptw(stops, bus_capacity=DEFAULT_BUS_CAPACITY,
                school_start="08:00", dismissal_time=None,
                school_lat=None, school_lng=None, trip="am", start_stop=None):
    if not stops:
        return []
    if school_lat is None or school_lng is None:
        school_lat = sum(float(s["lat"]) for s in stops) / len(stops)
        school_lng = sum(float(s["lng"]) for s in stops) / len(stops)

    n = len(stops)
    demand      = _build_demand(stops)
    h, m = map(int, school_start.split(":"))
    if trip == "pm":
        dh, dm = map(int, (dismissal_time or DEFAULT_DISMISSAL_TIME).split(":"))
        anchor_mins = dh * 60 + dm
    else:
        anchor_mins = h * 60 + m - ARRIVAL_BUFFER_MINS  # arrive early for traffic buffer
    time_max = anchor_mins + (0 if trip == "am" else 150)

    # Cost matrix in MINUTES from straight-line distance (500 m/min), used for
    # the TSP ordering. Deterministic and API-free — the per-leg driving API is
    # too slow/throttle-prone to call for every pair, and the PDF pickup times
    # are computed separately from real per-leg driving anyway. Ordering just
    # needs to be geographically sensible: farthest-from-school first for AM,
    # school first for PM.
    cost_matrix = _build_cost_matrix(stops, school_lat, school_lng)  # meters
    cost_matrix = [[m / 500.0 for m in row] for row in cost_matrix]  # → minutes

    # --- Local TSP ordering (no external solver dependency) ---
    # AMap's driving API does NOT optimise waypoint order — it routes them in the
    # order given. So we arrange the stops ourselves with nearest-neighbour + 2-opt
    # on the real road-driving-minute matrix, then feed that order to AMap.
    demands = demand[1:]  # students per stop
    total_students = sum(demands)

    if total_students <= bus_capacity:
        order = _tsp_order(n, 0, trip, cost_matrix, start_stop)
        routes = [[stops[i - 1].copy() for i in order]]
    else:
        # Capacity-split: greedily pack stops (nearest-to-school first), then
        # TSP-order each group so it ends near school.
        remaining = sorted(range(1, n + 1), key=lambda i: cost_matrix[0][i])
        groups, current, load = [], [], 0
        for i in remaining:
            if load + demands[i - 1] <= bus_capacity:
                current.append(i); load += demands[i - 1]
            else:
                groups.append(current); current, load = [i], demands[i - 1]
        if current:
            groups.append(current)
        # The group holding the driver-specified start stop runs first.
        if start_stop is not None:
            groups = sorted(groups, key=lambda g: 0 if start_stop in g else 1)
        routes = []
        for group in groups:
            ss = start_stop if start_stop in group else None
            pts = [0] + group
            sub = [[cost_matrix[a][b] for b in pts] for a in pts]
            order = _tsp_order(len(group), 0, trip, sub, ss)
            routes.append([stops[group[k - 1] - 1].copy() for k in order])

    for route in routes:
        _assign_times(route, anchor_mins, trip)

    print(f"  Local TSP: {len(routes)} routes, {sum(len(r) for r in routes)} stops")
    return routes


def _greedy_fallback(stops, bus_capacity, anchor_mins, trip="am"):
    remaining = list(stops)
    routes = []
    while remaining:
        route = []
        load = 0
        c_lat, c_lng = float(remaining[0]["lat"]), float(remaining[0]["lng"])
        while remaining and load < bus_capacity:
            best = min(remaining, key=lambda s: _haversine(c_lat, c_lng, float(s["lat"]), float(s["lng"])))
            if load + int(best["students"]) > bus_capacity:
                break
            route.append(best)
            load += int(best["students"])
            c_lat, c_lng = float(best["lat"]), float(best["lng"])
            remaining.remove(best)
        if route:
            _assign_times(route, anchor_mins, trip)
            routes.append(route)
    total = sum(int(s["students"]) for r in routes for s in r)
    print(f"  Greedy: {len(routes)} routes, {total} students")
    return routes


def _start_stop_index(stops, stop_id):
    """Map a driver-specified start stop_id to its 1-based TSP index (or None)."""
    if not stop_id:
        return None
    for i, s in enumerate(stops, 1):
        if s.get("stop_id") == stop_id:
            return i
    return None


def solve_all_schools(students_with_stops_csv, schools_csv=None, trip="am",
                      bus_capacity=DEFAULT_BUS_CAPACITY, start_stops=None):
    stops_csv = DATA_DIR / f"stops_{trip}.csv"
    if not Path(students_with_stops_csv).exists():
        print(f"  students_with_stops_{trip}.csv not found — skipping route building")
        return {}
    if not stops_csv.exists():
        print(f"  stops_{trip}.csv not found — skipping route building")
        return {}
    students = list(csv.DictReader(open(students_with_stops_csv, encoding="utf-8-sig")))
    stops_list = list(csv.DictReader(open(stops_csv, encoding="utf-8-sig")))
    schools = {}
    if schools_csv and Path(schools_csv).exists():
        for row in csv.DictReader(open(schools_csv, encoding="utf-8-sig")):
            schools[row["school"]] = row
    else:
        for sn in set(s.get("school", "") for s in students if s.get("school")):
            schools[sn] = {"school": sn, "start_time": "08:00", "lat": "", "lng": ""}

    school_stops = defaultdict(list)
    for stop in stops_list:
        school_stops[stop["school"]].append(stop)

    all_routes = {}
    for school_name, config in schools.items():
        print(f"\nSolving {school_name} ({trip.upper()})...")
        sch_lat = float(config["lat"]) if config.get("lat") else None
        sch_lng = float(config["lng"]) if config.get("lng") else None
        routes = solve_vrptw(
            school_stops.get(school_name, []),
            bus_capacity=bus_capacity,
            school_start=config.get("start_time", "08:00"),
            dismissal_time=config.get("dismissal_time"),
            school_lat=sch_lat, school_lng=sch_lng,
            trip=trip,
            start_stop=_start_stop_index(school_stops.get(school_name, []),
                                         (start_stops or {}).get(school_name)),
        )
        all_routes[school_name] = routes

    import json
    from driving import drive_all, drive_route, format_duration, DRIVING_FASTEST

    manifest_path = str(DATA_DIR / f"route_manifest_{trip}.csv")
    polyline_path = str(DATA_DIR / f"route_polylines_{trip}.json")
    steps_path    = str(DATA_DIR / f"route_steps_{trip}.json")
    polylines = {}
    steps_all = {}
    legs_all = {}
    rows = []

    for school, routes in all_routes.items():
        # School location: from config, else centroid of its stops
        cfg = schools.get(school, {})
        sch_lat = float(cfg["lat"]) if cfg.get("lat") else None
        sch_lng = float(cfg["lng"]) if cfg.get("lng") else None
        if sch_lat is None:
            ss = school_stops.get(school, [])
            if ss:
                sch_lat = sum(float(s["lat"]) for s in ss) / len(ss)
                sch_lng = sum(float(s["lng"]) for s in ss) / len(ss)

        for ri, route in enumerate(routes, 1):
            route_num = f"R{ri:02d}"
            if not route or sch_lat is None:
                continue
            school_pt = (sch_lat, sch_lng)
            if trip == "pm":
                # school -> dropoff stops (bus leaves school, drops off along the way)
                origin    = school_pt
                waypoints = [(float(s["lat"]), float(s["lng"])) for s in route[:-1]]
                dest      = (float(route[-1]["lat"]), float(route[-1]["lng"]))
                skip      = _haversine(*origin, *dest) < 80
            else:
                # pickup stops -> school
                origin    = (float(route[0]["lat"]), float(route[0]["lng"]))
                waypoints = [(float(s["lat"]), float(s["lng"])) for s in route[1:]]
                dest      = school_pt
                skip      = _haversine(*origin, *dest) < 80

            variants = {}
            if not skip:
                variants = drive_all(origin, waypoints, dest)

            # Compute realistic pickup/dropoff times from real per-leg driving
            # durations + fixed dwell time at each stop (requirement: timetable
            # must match actual driving sequence).
            h, m = map(int, cfg.get("start_time", "08:00").split(":"))
            if trip == "pm":
                dh, dm = map(int, (cfg.get("dismissal_time") or DEFAULT_DISMISSAL_TIME).split(":"))
                anchor_mins = dh * 60 + dm
            else:
                anchor_mins = h * 60 + m - ARRIVAL_BUFFER_MINS
            dwell = SERVICE_TIME_PER_STOP
            pts = [(float(s["lat"]), float(s["lng"])) for s in route]
            legs = []  # per-leg data for the per-leg maps

            def _leg_data(a, b):
                res = drive_route(a, [], b, DRIVING_FASTEST)
                if res:
                    return res
                return {"distance_m": int(_haversine(*a, *b)),
                        "duration_s": int(_haversine(*a, *b) / 500 * 60),
                        "polyline": ""}

            def _capture(a, b):
                leg = _leg_data(a, b)
                # main roads on this leg (for the instruction text)
                roads, seen = [], set()
                for st in leg.get("steps", []):
                    r = (st.get("road") or "").strip()
                    if r and r not in seen:
                        seen.add(r)
                        roads.append(r)
                legs.append({"origin": a, "dest": b,
                             "polyline": leg["polyline"],
                             "distance_m": leg["distance_m"],
                             "duration_s": leg["duration_s"],
                             "roads": roads[:3]})
                return leg["duration_s"] / 60.0

            if trip == "pm":
                seq = [school_pt] + pts
                t = anchor_mins
                for i in range(len(pts)):
                    t += _capture(seq[i], seq[i + 1])
                    route[i]["pickup_time"] = f"{int(t)//60:02d}:{int(t)%60:02d}"
                    t += dwell
            else:
                seq = pts + [school_pt]
                t = anchor_mins
                for i in range(len(pts) - 1, -1, -1):
                    t -= _capture(seq[i], seq[i + 1])
                    t -= dwell
                    route[i]["pickup_time"] = f"{int(t)//60:02d}:{int(t)%60:02d}"
                legs.reverse()  # AM legs were captured backward → put in route order

            legs_all[f"{school}|{route_num}"] = legs

            def _variant(label):
                r = variants.get(label)
                return {
                    f"{label}_duration":   format_duration(r["duration_s"]) if r else "—",
                    f"{label}_distance_m": r["distance_m"] if r else "",
                    f"{label}_tolls":      r["tolls"] if r else "",
                }
            stats = {
                "route_number": route_num,
                "trip": trip,
                **_variant("fastest"),
                **_variant("shortest"),
                **_variant("tollfree"),
            }
            key = f"{school}|{route_num}"
            polylines[key] = {label: (variants.get(label) or {}).get("polyline", "")
                              for label in ("fastest", "shortest", "tollfree")}
            steps_all[key] = {label: (variants.get(label) or {}).get("steps", [])
                              for label in ("fastest", "shortest", "tollfree")}
            print(f"  {school} {route_num} [{trip.upper()}]: "
                  f"fastest={stats['fastest_duration']} {stats['fastest_distance_m']}m | "
                  f"shortest={stats['shortest_duration']} {stats['shortest_distance_m']}m | "
                  f"tollfree={stats['tollfree_duration']} {stats['tollfree_distance_m']}m")

            for stop in route:
                rows.append({
                    "school": school, "route_number": route_num, "trip": trip,
                    "route_order": stop.get("route_order", ""),
                    "stop_id": stop["stop_id"],
                    "lat": stop["lat"], "lng": stop["lng"],
                    "pickup_time": stop.get("pickup_time", ""),
                    "students_at_stop": stop["students"],
                    **stats,
                })

    if polylines:
        with open(polyline_path, "w", encoding="utf-8") as f:
            json.dump(polylines, f, ensure_ascii=False)
        with open(steps_path, "w", encoding="utf-8") as f:
            json.dump(steps_all, f, ensure_ascii=False)
        with open(str(DATA_DIR / f"route_legs_{trip}.json"), "w", encoding="utf-8") as f:
            json.dump(legs_all, f, ensure_ascii=False)
    if rows:
        with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nManifest ({trip.upper()}): {manifest_path}")
    return all_routes


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python route.py <students_with_stops.csv> [schools.csv]")
        sys.exit(1)
    solve_all_schools(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)

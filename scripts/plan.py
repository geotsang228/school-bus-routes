"""Stop design (capacity-aware clustering) and route building (CVRPTW).

Uses Google OR-Tools when importable; falls back to a greedy + 2-opt heuristic
so the pipeline runs even on an environment where OR-Tools can't install.
Pickup times are back-calculated from the school start time using a speed
estimate; swap AVG_SPEED_KMH once real driving times are available.
"""
import math

import numpy as np

from config import (AVG_SPEED_KMH, BUS_CAPACITY, SERVICE_SECONDS, STOP_CAP,
                    haversine_m, travel_seconds, to_hhmm)

try:
    from ortools.constraint_solver import routing_enums_pb2, pywrapcp
    ORTOOLS = True
except Exception:
    ORTOOLS = False

# ----------------------------------------------------------------------------
# Clustering (numpy Lloyd's k-means, capacity split)
# ----------------------------------------------------------------------------

def _kmeans(coords, k, seed=0, iters=120):
    rng = np.random.default_rng(seed)
    n = len(coords)
    if n <= k:
        return np.arange(n), coords.astype(float).copy()
    idx = rng.choice(n, k, replace=False)
    centers = coords[idx].astype(float).copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        d = ((coords[:, None, :] - centers[None, :, :]) ** 2).sum(-1)
        new = d.argmin(1)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            m = labels == j
            centers[j] = coords[m].mean(0) if m.sum() else coords[rng.integers(n)]
    return labels, centers


def _stop_label(members, school_code, j):
    # Prefer the most common concrete address; else a district-based label.
    counts = {}
    for a in members["address"].dropna().astype(str):
        counts[a] = counts.get(a, 0) + 1
    if counts:
        return max(counts, key=counts.get)[:60]
    return f"Stop {school_code}-{j + 1:02d}"


def design_stops(students, stop_cap=STOP_CAP):
    """Cluster students into capacity-bounded stops. students has school/lat/lon/..."""
    stops = []
    for school, g in students.groupby("school"):
        g = g.reset_index(drop=True)
        coords = g[["lat", "lon"]].values.astype(float)
        n = len(g)
        if n == 0:
            continue
        code = "".join(c for c in school.split() if c.isalnum())[:6] or "SCH"
        k = max(1, math.ceil(n / stop_cap))
        labels, centers = _kmeans(coords, min(k, n))
        # split any cluster that still exceeds the per-stop cap
        while True:
            sizes = np.bincount(labels, minlength=len(centers))
            over = [j for j, s in enumerate(sizes) if s > stop_cap]
            if not over:
                break
            j = over[0]
            idx = np.where(labels == j)[0]
            sub_lab, sub_cent = _kmeans(coords[idx], min(2, len(idx)), seed=j)
            new_base = len(centers)
            centers = np.concatenate([centers, sub_cent], 0)
            for i2, orig in enumerate(idx):
                labels[orig] = new_base + sub_lab[i2]
        for j in range(len(centers)):
            idx = np.where(labels == j)[0]
            if len(idx) == 0:
                continue
            members = g.iloc[idx]
            stops.append({
                "stop_id": f"{code}-S{j + 1:02d}",
                "school": school,
                "name": _stop_label(members, code, j),
                "lat": round(float(centers[j][0]), 6),
                "lon": round(float(centers[j][1]), 6),
                "n_students": int(len(idx)),
                "students": members[["student_id", "name", "class", "address"]].to_dict("records"),
            })
    return stops


# ----------------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------------

def _time_matrix(stops_geo, school_geo, speed_kmh):
    pts = [school_geo] + stops_geo
    t = [[0.0] * len(pts) for _ in pts]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = haversine_m(*pts[i], *pts[j])
            s = travel_seconds(d, speed_kmh)
            t[i][j] = t[j][i] = s
    return t


def _solve_ortools(t, demands, capacity, deadline, service, time_limit_s=6.0):
    n = len(demands)
    vehicles = n  # upper bound; fixed cost forces the solver to use few
    manager = pywrapcp.RoutingIndexManager(n + 1, vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_cb(i, j):
        return int(round(t[manager.IndexToNode(i)][manager.IndexToNode(j)]))

    def dem_cb(i):
        node = manager.IndexToNode(i)
        return demands[node - 1] if node else 0

    time_idx = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(time_idx)
    routing.SetFixedCostOfAllVehicles(100_000)

    dem_idx = routing.RegisterUnaryTransitCallback(dem_cb)
    routing.AddDimensionWithVehicleCapacity(dem_idx, 0, [capacity] * vehicles, True, "capacity")

    routing.AddDimension(time_idx, 0, deadline, True, "time")
    time_dim = routing.GetDimensionOrDie("time")
    for i in range(1, n + 1):
        time_dim.CumulVar(i).SetRange(0, deadline)

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search.time_limit.seconds = time_limit_s
    sol = routing.SolveWithParameters(search)
    if not sol:
        return None

    routes = []
    for v in range(vehicles):
        if routing.IsVehicleUsed(sol, v):
            node = routing.Start(v)
            seq = []
            while not routing.IsEnd(node):
                n_ = manager.IndexToNode(node)
                if n_:
                    seq.append(n_ - 1)
                node = sol.Value(routing.NextVar(node))
            routes.append(seq)
    return routes


def _greedy_routes(t, demands, capacity, deadline, service):
    n = len(demands)
    remaining = set(range(1, n + 1))  # 0 is school depot
    routes = []
    while remaining:
        route, load, time_used, cur = [], 0, 0.0, 0
        while True:
            best = None
            for nd in remaining:
                if load + demands[nd - 1] > capacity:
                    continue
                arrival = time_used + t[cur][nd]
                if arrival + t[nd][0] > deadline:
                    continue
                if best is None or t[cur][nd] < t[cur][best]:
                    best = nd
            if best is None:
                break
            route.append(best)
            load += demands[best - 1]
            time_used += t[cur][best]
            cur = best
            remaining.discard(best)
        routes.append(route)
    return _two_opt_all(t, routes)


def _two_opt_all(t, routes):
    for route in routes:
        improved = True
        while improved:
            improved = False
            for i in range(len(route) - 1):
                for j in range(i + 1, len(route)):
                    a, b, c, d = route[i], route[i + 1], route[j], route[j + 1] if j + 1 < len(route) else 0
                    cur = t[a][b] + t[c][d]
                    new = t[a][c] + t[b][d]
                    if new < cur - 1e-9:
                        route[i + 1:j + 1] = reversed(route[i + 1:j + 1])
                        improved = True
    return routes


def _backward_times(stops_geo, school_geo, start_seconds, service, speed_kmh):
    """Pickup time per stop, computed backwards from the school arrival deadline."""
    times = {}
    prev = school_geo
    depart_prev = start_seconds
    for geo in reversed(stops_geo):
        arrive = depart_prev - travel_seconds(haversine_m(*geo, *prev), speed_kmh)
        times[tuple(geo)] = arrive
        depart_prev = arrive - service
        prev = geo
    return times


def build_routes(stops, school_geo_by_school, start_seconds, capacity=BUS_CAPACITY,
                 service=SERVICE_SECONDS, speed_kmh=AVG_SPEED_KMH, time_limit_s=6.0):
    """Returns list of route dicts with ordered stops + pickup times."""
    routes = []
    for school, sgroup in _group_by(stops, "school").items():
        depot = school_geo_by_school.get(school)
        if not depot:
            continue
        geos = [(s["lat"], s["lon"]) for s in sgroup]
        demands = [s["n_students"] for s in sgroup]
        t = _time_matrix(geos, depot, speed_kmh)
        deadline = start_seconds

        seqs = None
        if ORTOOLS:
            try:
                seqs = _solve_ortools(t, demands, capacity, deadline, service, time_limit_s)
            except Exception:
                seqs = None
        if seqs is None:
            seqs = _greedy_routes(t, demands, capacity, deadline, service)

        times = _backward_times(geos, depot, deadline, service, speed_kmh)
        for r_i, seq in enumerate(seqs, 1):
            route_stops = []
            load = 0
            dist = 0.0
            prev = depot
            for stop_i in seq:
                s = sgroup[stop_i]
                geo = (s["lat"], s["lon"])
                dist += haversine_m(*prev, *geo)
                prev = geo
                load += s["n_students"]
                route_stops.append({**s, "pickup": to_hhmm(times[geo])})
            dist += haversine_m(*prev, *depot)
            code = "".join(c for c in school.split() if c.isalnum())[:6] or "SCH"
            routes.append({
                "route_id": f"{code}-R{r_i:02d}",
                "school": school,
                "load": int(load),
                "distance_m": round(dist),
                "eta_school": to_hhmm(deadline),
                "stops": route_stops,
            })
    return routes


def _group_by(items, key):
    out = {}
    for it in items:
        out.setdefault(it[key], []).append(it)
    return out

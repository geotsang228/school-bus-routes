"""
School Bus Routes — stop design & student-to-stop assignment.
K-Means clustering, walk-radius filtering, capacity enforcement,
then snap each cluster centroid to the nearest safe bus/coach stop.
"""
import csv, math
from collections import defaultdict
from pathlib import Path
from config import WALK_RADIUS_METRES, DEFAULT_BUS_CAPACITY, DATA_DIR
from poi import find_safe_stop, describe_location, stop_street


def _stop_label(name, street="", landmark="", fallback=""):
    """Driver-facing stop name — the POI name as it is, lightly cleaned
    (公交站 suffixes removed, 總站 shortened). No street / 近-landmark
    embellishment: the map pin already shows the exact location."""
    n = name or ""
    for suf in ("（公交站）", "(公交站)", "（公交車站）", "(公交車站)", "公交站", "公共汽车站"):
        n = n.replace(suf, "")
    n = n.replace("總站", "站").replace("总站", "站")
    n = n.strip(" ，,、")
    if n.startswith("近"):
        n = n.lstrip("近").strip()
    return n or fallback or name or ""


def _haversine(lat1, lng1, lat2, lng2):
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lng2 - lng1)
    a  = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _kmeans(points, k, max_iter=100):
    """Simple K-Means on (lat, lng) list. Returns (centroids, labels)."""
    import random
    n = len(points)
    if k >= n:
        return points, list(range(n))

    centroids = random.sample(points, k)
    labels = [0] * n

    for _ in range(max_iter):
        changed = False
        for i, (lat, lng) in enumerate(points):
            best_j = min(range(k), key=lambda j: _haversine(lat, lng, centroids[j][0], centroids[j][1]))
            if best_j != labels[i]:
                labels[i] = best_j
                changed = True
        if not changed:
            break
        for j in range(k):
            members = [(points[i][0], points[i][1]) for i in range(n) if labels[i] == j]
            if members:
                centroids[j] = (sum(m[0] for m in members)/len(members),
                                sum(m[1] for m in members)/len(members))
    return centroids, labels


def _snap_to_nearest_stop(student, stops, radius=WALK_RADIUS_METRES):
    """Find the nearest existing stop within walk radius."""
    best, best_dist = None, float("inf")
    for stop in stops:
        d = _haversine(student["lat"], student["lng"], stop["lat"], stop["lng"])
        if d < best_dist:
            best, best_dist = stop, d
    if best and best_dist <= radius:
        return best
    return None


def _emit_stop(cluster, school, stop_id_counter):
    """Build a stop from a cluster of students; assign stop_id to each."""
    slat = sum(float(m["lat"]) for m in cluster) / len(cluster)
    slng = sum(float(m["lng"]) for m in cluster) / len(cluster)
    sid = f"ST{stop_id_counter[0]:03d}"
    stop_id_counter[0] += 1
    for m in cluster:
        m["stop_id"] = sid
    return {
        "stop_id": sid,
        "lat": round(slat, 6),
        "lng": round(slng, 6),
        "students": len(cluster),
        "school": school,
    }


def _cluster_school(members, bus_capacity, stop_id_counter, lat_key="lat", lng_key="lng",
                    walk_radius=WALK_RADIUS_METRES):
    """
    Cluster one school's students into stops, enforcing capacity and a
    maximum walk radius to the stop.

    Radius-based greedy: grow a stop from a dense seed, adding every student
    within `walk_radius` of the growing cluster centroid; split over-sized
    clusters. This is the real constraint: students walk <=500m to a safe spot.
    """
    pool = list(members)
    if not pool:
        return []

    def nbr_count(m):
        lm, ln = float(m[lat_key]), float(m[lng_key])
        return sum(1 for o in pool if _haversine(lm, ln, float(o[lat_key]), float(o[lng_key])) <= walk_radius)

    school = pool[0].get("school", "")
    stops = []

    while pool:
        # Seed = student with the most neighbours within the walk radius
        seed = max(pool, key=nbr_count)
        clat, clng = float(seed[lat_key]), float(seed[lng_key])
        cluster = []
        changed = True
        while changed:
            changed = False
            for m in list(pool):
                if _haversine(clat, clng, float(m[lat_key]), float(m[lng_key])) <= walk_radius:
                    cluster.append(m)
                    pool.remove(m)
                    changed = True
            if cluster:
                clat = sum(float(m[lat_key]) for m in cluster) / len(cluster)
                clng = sum(float(m[lng_key]) for m in cluster) / len(cluster)

        # Split if this cluster exceeds capacity
        if len(cluster) > bus_capacity:
            sub_k = max(1, math.ceil(len(cluster) / bus_capacity))
            sub_k = min(sub_k, len(cluster))
            sub_points = [(float(m[lat_key]), float(m[lng_key])) for m in cluster]
            _, sub_labels = _kmeans(sub_points, sub_k)
            sub_groups = defaultdict(list)
            for i, sl in enumerate(sub_labels):
                sub_groups[sl].append(cluster[i])
            for _, sg in sorted(sub_groups.items()):
                stops.append(_emit_stop(sg, school, stop_id_counter))
        else:
            stops.append(_emit_stop(cluster, school, stop_id_counter))
    return stops


def _write_outputs(stops, valid, trip):
    """Write per-trip stops CSV + students_with_stops CSV."""
    stops_path = str(DATA_DIR / f"stops_{trip}.csv")
    with open(stops_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stop_id","lat","lng","students","school","name","address","safe","desc","street","label"])
        w.writeheader()
        w.writerows(stops)
    out_path = str(DATA_DIR / f"students_with_stops_{trip}.csv")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        fieldnames = list(valid[0].keys()) + ["stop_id"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(valid)
    print(f"  Stops:    {stops_path}")
    print(f"  Students: {out_path}")


def design_stops(geocoded_csv, bus_capacity=DEFAULT_BUS_CAPACITY,
                 existing_stops_csv=None, trip="am", mode="clustered"):
    """
    Generate stops for each school.
    mode="clustered": cluster students → snap to POI → merge duplicates (default)
    mode="custom": each student's own address = one stop (no POI lookup, no clustering)
    trip="am" uses pickup coords; trip="pm" uses dropoff coords.
    Returns list of stop dicts + assigns stop_id to each student.
    """
    lat_key, lng_key = ("lat", "lng") if trip == "am" else ("dropoff_lat", "dropoff_lng")
    students = list(csv.DictReader(open(geocoded_csv, encoding="utf-8-sig")))

    # Filter to students with valid coordinates for this trip
    valid = [s for s in students if s.get(lat_key) and s.get(lng_key)]
    if not valid:
        print(f"No students with valid {trip} coordinates — cannot cluster.")
        return [], []

    # --- Custom mode: each student's address = its own stop, no POI lookup ---
    if mode == "custom":
        print(f"\nCustom mode: {len(valid)} students = {len(valid)} stops ({trip.upper()})")
        stops = []
        for s in valid:
            sid = s.get("student_id", "")
            stop = {
                "stop_id":  sid,
                "lat":       float(s[lat_key]),
                "lng":       float(s[lng_key]),
                "students":  1,
                "school":    s.get("school", ""),
                "name":      s.get("name", ""),
                "address":   s.get("address", ""),
                "safe":      "1",
                "desc":      s.get("address", ""),
                "street":    "",
            }
            s["stop_id"] = sid
            stops.append(stop)
        # Write stops + students (no snapping, no merge)
        _write_outputs(stops, valid, trip)
        return stops, valid

    # --- Clustered mode (default) ---
    by_school = defaultdict(list)
    for s in valid:
        by_school[s.get("school", "Unknown")].append(s)

    print(f"\nClustering {len(valid)} students by school ({trip.upper()})...")
    stops = []
    stop_id_counter = [1]
    for school, members in sorted(by_school.items()):
        school_stops = _cluster_school(members, bus_capacity, stop_id_counter, lat_key, lng_key)
        stops.extend(school_stops)
        print(f"  {school}: {len(members)} students → {len(school_stops)} stops")

    # Snap each cluster to the nearest reasonable stopping point (AMap POI)
    # within the walk radius. If none found, keep the cluster centroid (which is
    # within walk radius of every student) and name it by its area.
    print("  Finding reasonable pick-up points within 200m (fallback 450m)...")
    for stop in stops:
        spot = find_safe_stop(float(stop["lat"]), float(stop["lng"]),
                              radius=WALK_RADIUS_METRES)
        expanded = False
        if not spot:
            spot = find_safe_stop(float(stop["lat"]), float(stop["lng"]), radius=450)
            expanded = bool(spot)
        stop["desc"]   = describe_location(float(stop["lat"]), float(stop["lng"]))
        stop["street"] = stop_street(float(stop["lat"]), float(stop["lng"]))
        if spot:
            stop["name"]    = spot["name"]
            stop["address"] = spot["address"]
            stop["lat"]     = spot["lat"]
            stop["lng"]     = spot["lng"]
            stop["safe"]    = "1"
            how = " (450m fallback)" if expanded else ""
            print(f"    {stop['stop_id']} → {spot['name']} ({spot['distance']}m){how}")
        else:
            stop["name"]    = stop["desc"] or stop["stop_id"]  # area-based label
            stop["address"] = ""
            stop["safe"]    = "1"
            print(f"    {stop['stop_id']} → area stop ({stop['desc'] or 'no POI within 450m'})")
        stop["label"] = _stop_label(stop["name"], stop["street"], "", stop["desc"])

    # Merge stops that snapped to the SAME POI (duplicate location)
    merge_map = {}
    seen_pois = {}
    for s in list(stops):
        key = (round(float(s["lat"]), 4), round(float(s["lng"]), 4))
        if key in seen_pois:
            target = seen_pois[key]
            print(f"    {s['stop_id']} (duplicate POI) → merged into {target['stop_id']}")
            target["students"] = int(target["students"]) + int(s["students"])
            merge_map[s["stop_id"]] = target["stop_id"]
            stops = [x for x in stops if x is not s]
        else:
            seen_pois[key] = s

    # Update student stop_ids to reflect merges
    if merge_map:
        for s in valid:
            if s.get("stop_id") in merge_map:
                s["stop_id"] = merge_map[s["stop_id"]]

    _write_outputs(stops, valid, trip)
    return stops, valid


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python cluster.py <students_geocoded.csv> [bus_capacity]")
        sys.exit(1)
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BUS_CAPACITY
    design_stops(sys.argv[1], cap)

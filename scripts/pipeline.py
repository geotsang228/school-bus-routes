
"""
School Bus Routes — end-to-end pipeline orchestrator.
Usage: python pipeline.py <students_csv> [schools_csv]
"""
import csv, os, sys, time
from pathlib import Path

# Fix Windows console encoding for Chinese characters
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DATA_DIR, OUTPUT_DIR, DEFAULT_BUS_CAPACITY
from geocode  import geocode_students
from cluster  import design_stops
from route    import solve_all_schools
from make_pdfs import generate_all_pdfs


def _load_start_stops():
    """Load optional driver-specified start stops per school."""
    import json as _json
    _ss = DATA_DIR / "start_stops.json"
    if _ss.exists():
        try:
            return _json.load(open(_ss, encoding="utf-8"))
        except Exception:
            pass
    return {}


def plan_routes(students_csv, schools_csv=None, capacity=DEFAULT_BUS_CAPACITY, mode="clustered"):
    """Run Stages 1–4: geocode, cluster, route. Returns summary dict for the review UI."""
    print("=" * 60)
    print("School Bus Route Planner — Phase 1: Plan Routes")
    print("=" * 60)

    t0 = time.time()
    start_stops = _load_start_stops()

    # Stage 1: Intake & normalise
    print("\n[Stage 1] Data intake & normalisation")
    print(f"  Input: {students_csv}")
    students = list(csv.DictReader(open(students_csv, encoding="utf-8-sig")))
    print(f"  Loaded {len(students)} student records")
    cols = list(students[0].keys())
    print(f"  Columns: {cols}")

    schools_in_data = set(s.get("school", "") for s in students if s.get("school"))
    print(f"  Schools found: {schools_in_data}")

    districts = set(s.get("district", "") for s in students if s.get("district"))
    print(f"  Districts: {districts}")

    # Stage 2: Geocode (pickup + optional dropoff addresses)
    print("\n[Stage 2] Geocoding addresses")
    geocoded_csv = str(DATA_DIR / "students_geocoded.csv")
    geocode_students(Path(students_csv), geocoded_csv)
    print(f"  Output: {geocoded_csv}")

    # Stages 3–4: cluster + route for BOTH trips (AM pickup→school, PM school→dropoff)
    for trip in ("am", "pm"):
        suffix = trip.upper()
        print(f"\n{'='*30} TRIP {suffix} {'='*30}")

        # Stage 3: Cluster into stops
        print("\n[Stage 3] Stop design — clustering students")
        stops, valid_students = design_stops(geocoded_csv, capacity, trip=trip, mode=mode)

        if not stops:
            print("  No stops created — check geocoding results")
            continue

        # Stage 4: Route building
        print("\n[Stage 4] Route building — VRP per school")
        students_with_stops = str(DATA_DIR / f"students_with_stops_{trip}.csv")
        all_routes = solve_all_schools(students_with_stops, schools_csv, trip=trip,
                                       bus_capacity=capacity, start_stops=start_stops)
        for school, routes in all_routes.items():
            total = sum(int(s["students"]) for r in routes for s in r)
            print(f"  {school}: {len(routes)} routes, {total} students")

    elapsed = time.time() - t0
    print(f"\nRoute planning complete in {elapsed:.1f}s")

    # Build summary for the review UI
    return _build_route_summary(students_csv)


def generate_outputs(students_csv, schools_csv=None, capacity=DEFAULT_BUS_CAPACITY, mode="clustered"):
    """Run Stage 5 only: generate maps + PDFs from the route data already in data/."""
    print("=" * 60)
    print("School Bus Route Planner — Phase 2: Generate PDFs")
    print("=" * 60)

    t0 = time.time()
    for trip in ("am", "pm"):
        suffix = trip.upper()
        print(f"\n{'='*30} TRIP {suffix} {'='*30}")
        print("\n[Stage 5] Generating PDF route guides")
        manifest_csv = str(DATA_DIR / f"route_manifest_{trip}.csv")
        students_with_stops = str(DATA_DIR / f"students_with_stops_{trip}.csv")
        generate_all_pdfs(manifest_csv, students_with_stops, trip=trip)

    elapsed = time.time() - t0
    print(f"\nPDF generation complete in {elapsed:.1f}s")
    print(f"Outputs: {OUTPUT_DIR}")


def _build_route_summary(students_csv):
    """Read the manifest + stops CSVs and return a structured summary for the UI."""
    summary = {"am": None, "pm": None, "unmatched": [], "total_students": 0}

    students = list(csv.DictReader(open(students_csv, encoding="utf-8-sig")))
    summary["total_students"] = len(students)

    # Check for unmatched addresses
    unmatched_path = DATA_DIR / "unmatched_addresses.csv"
    if unmatched_path.exists():
        try:
            summary["unmatched"] = list(csv.DictReader(open(unmatched_path, encoding="utf-8-sig")))
        except Exception:
            pass

    for trip in ("am", "pm"):
        manifest_path = DATA_DIR / f"route_manifest_{trip}.csv"
        stops_path = DATA_DIR / f"stops_{trip}.csv"
        if not manifest_path.exists():
            continue

        manifest = list(csv.DictReader(open(manifest_path, encoding="utf-8-sig")))
        stops = {}
        if stops_path.exists():
            for s in csv.DictReader(open(stops_path, encoding="utf-8")):
                stops[s["stop_id"]] = s.get("label", "") or s.get("name", "")

        # Group manifest rows by route
        routes = {}
        for row in manifest:
            rn = row.get("route_number", "?")
            if rn not in routes:
                routes[rn] = {
                    "route_number": rn,
                    "school": row.get("school", ""),
                    "stops": [],
                    "fastest_duration": row.get("fastest_duration", ""),
                    "fastest_distance_m": row.get("fastest_distance_m", ""),
                    "fastest_tolls": row.get("fastest_tolls", ""),
                    "tollfree_duration": row.get("tollfree_duration", ""),
                    "tollfree_distance_m": row.get("tollfree_distance_m", ""),
                    "tollfree_tolls": row.get("tollfree_tolls", ""),
                }
            routes[rn]["stops"].append({
                "stop_id": row.get("stop_id", ""),
                "label": stops.get(row.get("stop_id", ""), row.get("stop_id", "")),
                "pickup_time": row.get("pickup_time", ""),
                "students_at_stop": row.get("students_at_stop", ""),
            })

        summary[trip] = list(routes.values())

    return summary


def run_pipeline(students_csv, schools_csv=None, capacity=DEFAULT_BUS_CAPACITY, mode="clustered"):
    """Run the full pipeline (Stages 1–5). Kept for CLI backward compatibility."""
    plan_routes(students_csv, schools_csv, capacity, mode)
    generate_outputs(students_csv, schools_csv, capacity, mode)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <students_csv> [schools_csv] [capacity]")
        sys.exit(1)
    schools = sys.argv[2] if len(sys.argv) > 2 else None
    capacity = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BUS_CAPACITY
    mode = sys.argv[4] if len(sys.argv) > 4 else "custom"
    run_pipeline(sys.argv[1], schools, capacity, mode)

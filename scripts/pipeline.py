
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


def run_pipeline(students_csv, schools_csv=None, capacity=DEFAULT_BUS_CAPACITY, mode="clustered"):
    print("=" * 60)
    print("School Bus Route Planner — Pipeline Run")
    print("=" * 60)

    t0 = time.time()

    # Optional driver-specified start stop per school: {"<school>": "<stop_id>"}
    import json as _json
    start_stops = {}
    _ss = DATA_DIR / "start_stops.json"
    if _ss.exists():
        try:
            start_stops = _json.load(open(_ss, encoding="utf-8"))
            print(f"  Start stops: {start_stops}")
        except Exception:
            start_stops = {}

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

    # Stages 3-5: cluster + route + PDF for BOTH trips (AM pickup→school, PM school→dropoff)
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

        # Stage 5: PDF generation
        print("\n[Stage 5] Generating PDF route guides")
        manifest_csv = str(DATA_DIR / f"route_manifest_{trip}.csv")
        generate_all_pdfs(manifest_csv, students_with_stops, trip=trip)

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"Pipeline complete in {elapsed:.1f}s")
    print(f"Outputs: {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <students_csv> [schools_csv] [capacity]")
        sys.exit(1)
    schools = sys.argv[2] if len(sys.argv) > 2 else None
    capacity = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BUS_CAPACITY
    mode = sys.argv[4] if len(sys.argv) > 4 else "custom"
    run_pipeline(sys.argv[1], schools, capacity, mode)

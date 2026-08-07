# School Bus Routes — CLAUDE.md

Yearly school-bus route planning: student spreadsheet → designated stops → van routes (capacity + school start-time aware) → bilingual TC/EN PDF route guides for drivers and caretakers. Recurs every year with a fresh student list.

## Data conventions

| File | Role |
| --- | --- |
| `data/students_<year>.csv` | Master student list (drop the real spreadsheet here, or give Claude the path). Sample: `data/sample_students.csv`. |
| `data/stops_last_year.csv` | Last year's stops (optional — hybrid input so stable stops are reused). |
| `data/manual_locations.csv` | Curated overrides for addresses AMap gets wrong/misses (Claude or user edits). Loaded with highest priority. |
| `data/geocode_cache.csv` | Auto-built deterministic cache — first good geocode per address is locked in. Makes runs repeatable. |
| `data/students_geocoded.csv` | Stage 2 output: students + lat/lng + geocode_source (amap/osm/manual/cache/mock). |
| `data/stops.csv`, `data/students_with_stops.csv` | Stage 3 output: stops (snapped to safe bus/coach stops) + student→stop assignment. |
| `data/route_manifest.csv` | Stage 4 output: routes with ordered stops, pickup times, **fastest + toll-free** drive stats. |
| `data/route_polylines.json` | Route geometry per variant (for the real map). |
| `data/schools.csv` | School locations + start times. `school,lat,lng,start_time`. |

## Pipeline

0. Inspect a sample of the spreadsheet → confirm columns, school start times, bus capacities.
1. Normalize → clean CSV.
2. Geocode via AMap. **AMap HK geocoding is unreliable**: it occasionally returns mainland-China coords, and results vary run-to-run. Mitigations built in: (a) HK bounding-box check rejects out-of-HK results, (b) `geocode_cache.csv` locks in first good result, (c) `manual_locations.csv` overrides, (d) OSM/Nominatim fallback. Target 100% (mock = unresolved).
3. Stop design per school — cluster students (K-Means) with capacity caps, then **snap each cluster centroid to the nearest safe bus/coach stop** (AMap POI search, type-filtered to 公交车站/长途汽车站; falls back to centroid). Enforce students/route ≤ bus capacity.
4. Route building per school — OR-Tools CVRPTW (capacity + school start-time deadline), haversine cost for ordering. Then call **AMap driving per route twice**: strategy 0 (fastest) and strategy 7 (toll-free) → real duration/distance/tolls + polyline.
5. Route manifests → Google Sheets for human review and adjustment (via google-workspace MCP).
6. HTML/CSS → PDF (WeasyPrint on Linux/macOS; **headless Edge** on Windows). Each sheet shows a **two-variant bar** (Fastest 最快 vs Toll-free 免路費: duration, distance, toll), the stop table, and a **real AMap static map** with the highlighted route polyline + numbered stop markers (SVG schematic as offline fallback).
7. Deliver: stops sheet, manifests, per-route PDFs.

## Running it

- `python scripts/pipeline.py data/students_<year>.csv data/schools.csv` — end-to-end (geocode → snap stops → route → fastest/toll-free → PDF with map).
- `python scripts/make_pdfs.py` — regenerate PDFs after manifest edits.
- All stages verified working on `data/sample_students.csv` + `data/schools.csv` with a real AMap key.

## Gotchas learned

- **AMap static map `paths` requires the two empty placeholder fields** (`weight,color,transparency,,:lng,lat;…`). Omitting them returns error 20003 even when the 靜態地圖 permission is enabled.
- **markersStyle order is `size,color,label`**; labels are single chars only (`[0-9]`, `[A-Z]`, one Chinese char). Max 10 markers.
- AMap free-tier throttles rapid bursts (error 10021) — the driving client adds delays + retries.

## Credentials

- AMap free key (个人开发者, covers HK) — store in `scripts/.env` as `AMAP_KEY`. Never commit.
- Google Workspace via MCP (Sheets/Drive) — existing setup in Cowork OS.

## Editorial rules

- Bilingual Traditional Chinese + English output.
- Addresses taken verbatim from source; never guess or invent location details.
- Keep the driver/caretaker sheets minimal and large-print — they're read on the move.

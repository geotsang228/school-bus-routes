"""
RRIFA — Re-Route It For All
School Bus Route Planner — Streamlit web app for non-technical colleagues.

Usage (local):  streamlit run app.py
Usage (cloud):  deploy on Streamlit Community Cloud — see README.md

Flow:  upload Excel → plan routes (fast) → review → generate PDFs (slow) → download.
Bilingual Traditional Chinese / English.
"""
import csv
import io
import json
import os
import sys
import threading
import time as _time
import zipfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

# --- AMap key bootstrap ---
try:
    os.environ.setdefault("AMAP_KEY", st.secrets["amap_key"])
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import DATA_DIR, OUTPUT_DIR  # noqa: E402
import normalize_xlsx  # noqa: E402
import pipeline  # noqa: E402

SCHOOLS_CSV = DATA_DIR / "schools.csv"
TEMP_SCHOOLS_CSV = DATA_DIR / "schools_temp.csv"
UPLOADED_CSV = DATA_DIR / "students_uploaded.csv"

# --- Session state ---
if "phase" not in st.session_state:
    st.session_state.phase = "upload"
if "summary" not in st.session_state:
    st.session_state.summary = None
if "students_rows" not in st.session_state:
    st.session_state.students_rows = []
if "students_csv" not in st.session_state:
    st.session_state.students_csv = None
if "output_before" not in st.session_state:
    st.session_state.output_before = {}
if "student_details" not in st.session_state:
    st.session_state.student_details = []  # for the review table

STAGES = [
    ("upload",   "📤 上載 Excel"),
    ("plan",     "🗺️ 規劃路線"),
    ("review",   "📋 檢視路線"),
    ("generate", "📄 生成 PDF"),
    ("download", "📥 下載"),
]


def _fmt_mmss(seconds):
    m, s = int(seconds) // 60, int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def _build_summary(students_csv):
    """Read manifest + stops CSVs → structured summary for review."""
    summary = {"am": None, "pm": None, "unmatched": [], "total_students": 0}
    students = list(csv.DictReader(open(students_csv, encoding="utf-8-sig")))
    summary["total_students"] = len(students)

    unmatched_path = DATA_DIR / "unmatched_addresses.csv"
    if unmatched_path.exists():
        try:
            summary["unmatched"] = list(csv.DictReader(
                open(unmatched_path, encoding="utf-8-sig")))
        except Exception:
            pass

    for trip in ("am", "pm"):
        manifest_path = DATA_DIR / f"route_manifest_{trip}.csv"
        stops_path = DATA_DIR / f"stops_{trip}.csv"
        if not manifest_path.exists():
            continue
        manifest = list(csv.DictReader(
            open(manifest_path, encoding="utf-8-sig")))
        stops = {}
        if stops_path.exists():
            for s in csv.DictReader(open(stops_path, encoding="utf-8")):
                stops[s["stop_id"]] = {
                    "label": s.get("label", "") or s.get("name", ""),
                    "address": s.get("address", ""),
                    "lat": s.get("lat", ""),
                    "lng": s.get("lng", ""),
                }

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
            sid = row.get("stop_id", "")
            stop_info = stops.get(sid, {})
            routes[rn]["stops"].append({
                "stop_id": sid,
                "label": stop_info.get("label", sid),
                "address": stop_info.get("address", ""),
                "lat": stop_info.get("lat", ""),
                "lng": stop_info.get("lng", ""),
                "pickup_time": row.get("pickup_time", ""),
                "students_at_stop": row.get("students_at_stop", ""),
            })
        summary[trip] = list(routes.values())
    return summary


def _find_nearest_stop(stops_list, driver_lat, driver_lng):
    """Find the stop nearest to the driver's starting coordinates."""
    import math
    best, best_dist = None, float("inf")
    for s in stops_list:
        try:
            slat, slng = float(s["lat"]), float(s["lng"])
        except (ValueError, KeyError):
            continue
        d = math.sqrt((slat - driver_lat) ** 2 + (slng - driver_lng) ** 2)
        if d < best_dist:
            best, best_dist = s, d
    return best


def _geocode_address(address):
    """Geocode a single address via AMap → (lat, lng) or None."""
    try:
        from config import AMAP_KEY
        import requests
        resp = requests.get(
            "https://restapi.amap.com/v3/geocode/geo",
            params={"key": AMAP_KEY, "address": address, "city": "香港"},
            timeout=10,
        )
        data = resp.json()
        if data.get("geocodes"):
            loc = data["geocodes"][0]["location"].split(",")
            return float(loc[1]), float(loc[0])
    except Exception:
        pass
    return None


def _run_with_timer(fn, label, estimate, *args, **kwargs):
    """Run fn() in a background thread while showing an elapsed-time counter.

    This keeps the Streamlit UI responsive and shows the user the app is
    working, not hung. The timer updates every second via an st.empty().
    """
    result = [None]
    error = [None]

    def worker():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    timer = st.empty()
    t0 = _time.time()
    while t.is_alive():
        elapsed = int(_time.time() - t0)
        m, s = divmod(elapsed, 60)
        timer.caption(f"⏱️ {m:02d}:{s:02d} / 預計 {estimate}")
        _time.sleep(1)
    timer.empty()

    if error[0]:
        raise error[0]
    return result[0]


def _make_schools_csv(am_time, pm_time):
    """Create a temp schools CSV with user-specified start/end times."""
    # Read the real schools.csv for lat/lng
    schools = {}
    if SCHOOLS_CSV.exists():
        for row in csv.DictReader(open(SCHOOLS_CSV, encoding="utf-8-sig")):
            schools[row["school"]] = row

    # Override times
    for sch in schools.values():
        sch["start_time"] = am_time  # AM arrival deadline
        # PM uses a different column — the route solver reads start_time for AM
        # For PM, we store end_time separately; the pipeline handles it

    fieldnames = ["school", "lat", "lng", "start_time", "school_cn", "address"]
    with open(TEMP_SCHOOLS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(schools.values())
    return str(TEMP_SCHOOLS_CSV)


def _build_student_details(students_csv):
    """Build student detail rows with geocoded pickup/dropoff info."""
    rows = list(csv.DictReader(open(students_csv, encoding="utf-8-sig")))
    geocoded_path = DATA_DIR / "students_geocoded.csv"
    geo = {}
    if geocoded_path.exists():
        for r in csv.DictReader(open(geocoded_path, encoding="utf-8-sig")):
            geo[r.get("student_id", "")] = r

    details = []
    for s in rows:
        sid = s.get("student_id", "")
        g = geo.get(sid, {})
        details.append({
            "student_id": sid,
            "name": s.get("name", ""),
            "contact_phone": s.get("contact_phone", ""),
            "address": s.get("address", ""),
            "pickup_lat": g.get("lat", ""),
            "pickup_lng": g.get("lng", ""),
            "geocode_source": g.get("geocode_source", ""),
        })
    return details


# =========================================================================
# SIDEBAR — Stage progress
# =========================================================================
st.set_page_config(page_title="RRIFA — 校巴路線規劃", layout="wide")

with st.sidebar:
    st.header("🚌 RRIFA")
    st.caption("Re-Route It For All")
    st.divider()

    current_idx = next(
        (i for i, (k, _) in enumerate(STAGES) if k == st.session_state.phase), 0
    )
    for i, (key, label) in enumerate(STAGES):
        if i < current_idx:
            st.markdown(f"✅ {label}")
        elif i == current_idx:
            st.markdown(f"▶️ **{label}**")
        else:
            st.markdown(f"⬜ {label}")

    st.divider()
    st.caption("RRIFA — Re-Route It For All")

# =========================================================================
# MAIN
# =========================================================================
st.title("🚌 RRIFA — 校巴路線規劃")

# =========================================================================
# STAGE 1: UPLOAD
# =========================================================================
uploaded = st.file_uploader(
    "上載學生名單 Excel / Upload student list (.xlsx)",
    type=["xlsx"],
)

if uploaded is not None and not st.session_state.students_rows:
    tmp = DATA_DIR / "uploaded.xlsx"
    tmp.write_bytes(uploaded.getvalue())
    try:
        header, raw_rows = normalize_xlsx.read_xlsx(tmp)
        rows = [
            normalize_xlsx.to_pipeline_row(r, header) for r in raw_rows
            if normalize_xlsx.to_pipeline_row(r, header)["student_id"]
        ]
        st.session_state.students_rows = rows
    except Exception as e:
        st.error(f"讀取 Excel 失敗 / Failed to read Excel: {e}")
        st.stop()

    if not rows:
        st.warning("Excel 內沒有有效學生紀錄 / No valid student records found.")
        st.stop()

    schools_count = {}
    for s in rows:
        sch = s["school"] or "（未填學校 / no school）"
        schools_count[sch] = schools_count.get(sch, 0) + 1

    st.success(f"✅ 讀取 {len(rows)} 條學生紀錄 / Loaded {len(rows)} student records.")
    st.caption("學校分佈 / Schools: " + ", ".join(
        f"{k}: {v}" for k, v in schools_count.items()))

    preview = [
        {"學號 ID": s["student_id"], "姓名 Name": s["name"],
         "學校 School": s["school"], "地址 Address": s["address"]}
        for s in rows
    ]
    st.dataframe(preview, width="stretch", hide_index=True)

# =========================================================================
# SETTINGS — below upload
# =========================================================================
if st.session_state.students_rows:
    st.divider()
    st.subheader("⚙️ 路線設定 / Route settings")

    # Row 1: Capacity + Mode
    c1, c2 = st.columns(2)
    with c1:
        bus_capacity = st.selectbox(
            "🚌 每車座位 / Seats per bus",
            options=[16, 28, 25],
            index=0,
        )
    with c2:
        mode = st.radio(
            "📍 規劃方式 / Planning mode",
            options=["clustered", "custom"],
            format_func=lambda m: {
                "clustered": "聚類站點 Clustered stops",
                "custom": "每站一人 Each student = own stop",
            }[m],
            horizontal=True,
        )

    if mode == "clustered":
        st.info(
            "🔹 **聚類站點模式**：系統將附近（**200 米內**）的學生自動分組到同一個上車點。"
            "站點可設在任何安全的上車位置，不只限於巴士站。\n\n"
            "**Clustered mode**: students within **200m** are grouped into shared "
            "pick-up stops. Stops can be anywhere safe for the school bus to stop."
        )
    else:
        st.info(
            "🔹 **每人一站模式**：每位學生有獨立的上車點，設在離家 **50 米內**。\n\n"
            "**Own-stop mode**: each student gets an individual pick-up stop within "
            "**50m** of their home address."
        )

    # Row 2: Times + Driver start
    c3, c4 = st.columns(2)
    with c3:
        am_start = st.time_input(
            "🌅 AM 到校時間 / School start time (must arrive by)",
            value=None,
            help="校車必須在這時間前到達 / Bus must arrive before this time",
        )
        am_start_str = am_start.strftime("%H:%M") if am_start else "08:00"
    with c4:
        pm_end = st.time_input(
            "🌇 PM 放學時間 / School end time (bus departs)",
            value=None,
            help="校車在這時間離開學校 / Bus leaves school at this time",
        )
        pm_end_str = pm_end.strftime("%H:%M") if pm_end else "15:30"

    driver_start = st.text_input(
        "🚗 司機起點 / Driver start location (Stop 1)",
        placeholder="例如：大埔寶雅苑 / e.g. Po Nga Court, Tai Po",
        help="填寫後此地點會成為路線的第1個站。留空則由系統自動決定。"
             " / Becomes Stop 1 of the route. Leave blank for auto.",
    )

    # Plan button
    if st.session_state.phase in ("upload", "review"):
        btn_label = "🗺️ 重新規劃 / Re-plan" if st.session_state.phase == "review" else "🗺️ 規劃路線 / Plan routes"
        btn_type = "secondary" if st.session_state.phase == "review" else "primary"

        if st.button(btn_label, type=btn_type):
            # Write student CSV
            fieldnames = ["student_id", "name", "name_en", "school", "class_year",
                          "address", "dropoff_address", "district",
                          "contact_phone", "contact_name"]
            with open(UPLOADED_CSV, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(st.session_state.students_rows)
            st.session_state.students_csv = str(UPLOADED_CSV)

            # Create temp schools CSV with user-specified times
            schools_path = _make_schools_csv(am_start_str, pm_end_str)

            # Save driver start
            if driver_start.strip():
                start_stops_path = DATA_DIR / "start_stops.json"
                ss = {}
                if start_stops_path.exists():
                    try:
                        ss = json.load(open(start_stops_path, encoding="utf-8"))
                    except Exception:
                        pass
                # Will be matched to nearest stop after planning
                ss["_driver_start_address"] = driver_start.strip()
                json.dump(ss, open(start_stops_path, "w", encoding="utf-8"),
                          ensure_ascii=False)

            st.subheader("⏳ 路線規劃中… / Planning routes…")
            import time as _time_mod
            t0 = _time_mod.time()
            _run_with_timer(
                pipeline.plan_routes, "规划路線", "1–3 分鐘",
                str(UPLOADED_CSV), schools_path,
                capacity=int(bus_capacity), mode=mode,
            )
            st.session_state["_plan_elapsed"] = _time_mod.time() - t0

            # Match driver start to nearest stop
            if driver_start.strip():
                coords = _geocode_address(driver_start.strip())
                if coords:
                    dlat, dlng = coords
                    for trip in ("am", "pm"):
                        stops_path = DATA_DIR / f"stops_{trip}.csv"
                        if stops_path.exists():
                            all_stops = list(csv.DictReader(
                                open(stops_path, encoding="utf-8")))
                            nearest = _find_nearest_stop(all_stops, dlat, dlng)
                            if nearest:
                                manifest_path = DATA_DIR / f"route_manifest_{trip}.csv"
                                if manifest_path.exists():
                                    manifest = list(csv.DictReader(
                                        open(manifest_path, encoding="utf-8-sig")))
                                    schools = set(r.get("school", "") for r in manifest)
                                    start_stops_path = DATA_DIR / "start_stops.json"
                                    ss = {}
                                    if start_stops_path.exists():
                                        try:
                                            ss = json.load(open(start_stops_path, encoding="utf-8"))
                                        except Exception:
                                            pass
                                    for sch in schools:
                                        if sch:
                                            ss[sch] = nearest["stop_id"]
                                    ss.pop("_driver_start_address", None)
                                    json.dump(ss, open(start_stops_path, "w", encoding="utf-8"),
                                              ensure_ascii=False)

                    _run_with_timer(
                        pipeline.plan_routes, "重新排序", "1 分鐘",
                        str(UPLOADED_CSV), schools_path,
                        capacity=int(bus_capacity), mode=mode,
                    )

            st.session_state.summary = _build_summary(str(UPLOADED_CSV))
            st.session_state.student_details = _build_student_details(str(UPLOADED_CSV))
            st.session_state.phase = "review"
            st.rerun()

# =========================================================================
# STAGE 2: REVIEW
# =========================================================================
if st.session_state.phase == "review" and st.session_state.summary:
    summary = st.session_state.summary
    st.success(
        f"✅ 路線規劃完成 / Routes planned "
        f"({summary['total_students']} students, "
        f"{_fmt_mmss(st.session_state.get('_plan_elapsed', 0))})"
    )

    # --- Student details table ---
    if st.session_state.student_details:
        st.subheader("👤 學生資料 / Student details")
        details = st.session_state.student_details
        st.dataframe(
            [{"學生 Student": d["name"],
              "聯絡電話 Contact": d["contact_phone"],
              "地址 Address": d["address"],
              "經緯度 Lat,Lng": f"{d['pickup_lat']},{d['pickup_lng']}" if d["pickup_lat"] else "—",
              "來源 Source": d["geocode_source"]}
             for d in details],
            width="stretch", hide_index=True,
        )

    # --- Unmatched addresses ---
    unmatched = summary.get("unmatched", [])
    if unmatched:
        st.warning(
            f"⚠️ {len(unmatched)} 個地址未能識別 / "
            f"{len(unmatched)} addresses could not be geocoded"
        )
        with st.expander("查看未識別地址 / View unmatched addresses", expanded=True):
            st.dataframe(
                [{"學生 Student": r.get("name", ""),
                  "地址 Address": r.get("address", "")} for r in unmatched],
                width="stretch", hide_index=True,
            )

    # --- Route summary per trip ---
    flagged_stops = []
    for trip_label, trip_key in [("🌅 AM 早上接送", "am"), ("🌇 PM 下午接送", "pm")]:
        routes = summary.get(trip_key)
        if not routes:
            continue
        st.subheader(trip_label)

        for route in routes:
            rn = route["route_number"]
            n_stops = len(route["stops"])
            n_students = sum(
                int(s.get("students_at_stop", 0)) for s in route["stops"]
            )

            st.markdown(
                f"**🚐 Route {rn}** — {route.get('school', '')} "
                f"({n_students} students, {n_stops} stops)  \n"
                f"最快 {route.get('fastest_duration', '?')} · "
                f"免路費 {route.get('tollfree_duration', '?')}"
            )

            # Stop table with checkboxes + student names
            # Build student→stop mapping from students_with_stops CSV
            sws_path = DATA_DIR / f"students_with_stops_{trip_key}.csv"
            stop_students_map = {}
            if sws_path.exists():
                for r in csv.DictReader(open(sws_path, encoding="utf-8-sig")):
                    sid = r.get("stop_id", "")
                    if sid not in stop_students_map:
                        stop_students_map[sid] = []
                    stop_students_map[sid].append(r.get("name", ""))

            for idx, s in enumerate(route["stops"]):
                stop_label = s.get("label", s["stop_id"])
                stop_addr = s.get("address", "")
                stop_time = s.get("pickup_time", "")
                stop_students = s.get("students_at_stop", "?")
                student_names = stop_students_map.get(s["stop_id"], [])

                col_check, col_info = st.columns([1, 8])
                with col_check:
                    flagged = st.checkbox(
                        "⚠️",
                        key=f"flag_{trip_key}_{rn}_{s['stop_id']}",
                        help="標記此站點需要修改 / Flag this stop for changes",
                    )
                with col_info:
                    st.markdown(
                        f"**{idx + 1}. {stop_label}** "
                        f"({stop_students} students, {stop_time})"
                    )
                    if stop_addr:
                        st.caption(f"📍 {stop_addr}")
                    if student_names:
                        st.caption(f"👤 {', '.join(student_names)}")

                if flagged:
                    new_addr = st.text_input(
                        "✏️ 建議新地點 / Suggested new location",
                        key=f"addr_{trip_key}_{rn}_{s['stop_id']}",
                        placeholder="輸入新地址或地點名稱 / Enter new address or place name",
                    )
                    flagged_stops.append({
                        "stop_id": s["stop_id"],
                        "label": stop_label,
                        "trip": trip_key,
                        "route": rn,
                        "suggested_address": new_addr,
                    })

            st.divider()

    # --- Flagged stops summary ---
    if flagged_stops:
        st.warning(
            f"⚠️ 已標記 {len(flagged_stops)} 個站點 / "
            f"{len(flagged_stops)} stops flagged"
        )
        if st.button("📝 儲存標記並重新規劃 / Save flags & re-plan", type="secondary"):
            manual_path = DATA_DIR / "manual_locations.csv"
            existing = []
            if manual_path.exists():
                try:
                    existing = list(csv.DictReader(open(manual_path, encoding="utf-8-sig")))
                except Exception:
                    pass
            existing_ids = {r.get("stop_id", "") for r in existing}
            for fs in flagged_stops:
                if fs["suggested_address"] and fs["stop_id"] not in existing_ids:
                    existing.append({
                        "stop_id": fs["stop_id"],
                        "address": fs["suggested_address"],
                        "source": "manual",
                    })
            if existing:
                with open(manual_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=["stop_id", "address", "source"])
                    w.writeheader()
                    w.writerows(existing)
            st.rerun()

    # --- Generate button ---
    if st.button("📄 生成 PDF / Generate PDFs", type="primary"):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        st.session_state.output_before = {
            p: p.stat().st_mtime for p in OUTPUT_DIR.iterdir() if p.is_file()
        }

        st.subheader("⏳ 生成 PDF 中… / Generating PDFs…")
        _run_with_timer(
            pipeline.generate_outputs, "生成 PDF", "2–5 分鐘",
            st.session_state.students_csv, str(SCHOOLS_CSV),
            capacity=int(bus_capacity), mode=mode,
        )

        st.session_state.phase = "download"
        st.rerun()

# =========================================================================
# STAGE 3: DOWNLOAD + VIEW
# =========================================================================
if st.session_state.phase == "download":
    st.success("✅ PDF 生成完成 / PDFs ready!")

    pdf_files = sorted(OUTPUT_DIR.glob("*.pdf"))
    html_files = sorted(OUTPUT_DIR.glob("*.html"))
    before = st.session_state.get("output_before", {})
    recent_pdfs = [p for p in pdf_files
                   if p.name not in before or p.stat().st_mtime > before.get(p.name, 0)]
    recent_htmls = [p for p in html_files
                    if p.name not in before or p.stat().st_mtime > before.get(p.name, 0)]

    # --- Inline PDF viewer ---
    if recent_htmls:
        st.header("📖 路線指南 / Route guides")
        for f in sorted(recent_htmls, key=lambda p: p.name):
            with st.expander(f"📄 {f.name}", expanded=False):
                html_content = f.read_text(encoding="utf-8")
                # Render inline — tall enough for the full document
                components.html(html_content, height=800, scrolling=True)

    # --- Download buttons ---
    if recent_pdfs or recent_htmls:
        st.header("📥 下載 / Download")
        st.caption("請在工作階段結束前下載 — 系統每次運行都會覆寫結果。")
        all_recent = sorted(set(recent_pdfs + recent_htmls), key=lambda p: p.name)
        cols = st.columns(3)
        for i, f in enumerate(all_recent):
            with cols[i % 3]:
                data = f.read_bytes()
                st.download_button(
                    label=f"⬇️ {f.name}",
                    data=data,
                    file_name=f.name,
                    mime="application/pdf" if f.suffix == ".pdf" else "text/html",
                    key=str(f),
                )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in all_recent:
                z.write(f, f.name)
        st.download_button(
            label="📦 下載全部 (ZIP) / Download all (ZIP)",
            data=buf.getvalue(),
            file_name="school-bus-routes.zip",
            mime="application/zip",
        )

    # --- Manifest table ---
    st.divider()
    st.header("📋 路線總覽 / Route manifest")
    for trip in ("am", "pm"):
        mp = DATA_DIR / f"route_manifest_{trip}.csv"
        if mp.exists():
            df_rows = list(csv.DictReader(open(mp, encoding="utf-8-sig")))
            if df_rows:
                st.subheader(
                    f"{'AM 早上接送' if trip == 'am' else 'PM 下午接送'}")
                cols_keep = ["route_number", "stop_id", "pickup_time",
                             "students_at_stop", "fastest_duration",
                             "fastest_distance_m", "fastest_tolls",
                             "tollfree_duration", "tollfree_distance_m",
                             "tollfree_tolls"]
                view = [{c: r.get(c, "") for c in cols_keep} for r in df_rows]
                st.dataframe(view, width="stretch", hide_index=True)

    if st.button("🔄 開始新一輪 / Plan another round"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

st.caption("RRIFA — Re-Route It For All | Admin: see README.md")

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
import zipfile
from pathlib import Path

import streamlit as st

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
# SETTINGS — below upload, always visible when data is loaded
# =========================================================================
if st.session_state.students_rows:
    st.divider()
    st.subheader("⚙️ 路線設定 / Route settings")

    c1, c2 = st.columns(2)
    with c1:
        bus_capacity = st.selectbox(
            "🚌 每車座位 / Seats per bus",
            options=[16, 28, 25],
            index=0,
            help="16 座小巴 / 28 座中巴 / 25 系統預設",
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

    # Mode descriptions
    if mode == "clustered":
        st.info(
            "🔹 **聚類站點模式**：系統將附近（**200 米內**）的學生自動分組到同一個上車點。"
            "站點會自動匹配到最近的安全巴士站（公交站）。\n\n"
            "**Clustered mode**: students within **200m** of each other are grouped "
            "into shared pick-up stops. Stops are auto-matched to the nearest safe "
            "bus/coach stop (公交站)."
        )
    else:
        st.info(
            "🔹 **每人一站模式**：每位學生有獨立的上車點，設在離家 **50 米內**。"
            "站點會匹配到最近的公交站。\n\n"
            "**Own-stop mode**: each student gets an individual pick-up stop within "
            "**50m** of their home address, matched to the nearest bus stop."
        )

    # Plan button
    if st.session_state.phase in ("upload", "review"):
        btn_label = "🗺️ 重新規劃 / Re-plan" if st.session_state.phase == "review" else "🗺️ 規劃路線 / Plan routes"
        btn_type = "secondary" if st.session_state.phase == "review" else "primary"

        if st.button(btn_label, type=btn_type):
            # Write CSV
            fieldnames = ["student_id", "name", "name_en", "school", "class_year",
                          "address", "dropoff_address", "district",
                          "contact_phone", "contact_name"]
            with open(UPLOADED_CSV, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(st.session_state.students_rows)
            st.session_state.students_csv = str(UPLOADED_CSV)

            with st.spinner("⏳ 路線規劃中（約 1–3 分鐘）… / Planning routes…"):
                import time
                t0 = time.time()
                pipeline.plan_routes(
                    str(UPLOADED_CSV), str(SCHOOLS_CSV),
                    capacity=int(bus_capacity), mode=mode,
                )
                st.session_state["_plan_elapsed"] = time.time() - t0

            st.session_state.summary = _build_summary(str(UPLOADED_CSV))
            st.session_state.phase = "review"
            st.rerun()

# =========================================================================
# STAGE 3: REVIEW — stops with feedback checkboxes
# =========================================================================
if st.session_state.phase == "review" and st.session_state.summary:
    summary = st.session_state.summary
    st.success(
        f"✅ 路線規劃完成 / Routes planned "
        f"({summary['total_students']} students, "
        f"{_fmt_mmss(st.session_state.get('_plan_elapsed', 0))})"
    )

    # Unmatched addresses
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

    # Route summary per trip
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

            # Stop table with checkboxes
            for idx, s in enumerate(route["stops"]):
                stop_label = s.get("label", s["stop_id"])
                stop_addr = s.get("address", "")
                stop_time = s.get("pickup_time", "")
                stop_students = s.get("students_at_stop", "?")

                col_check, col_info = st.columns([1, 8])
                with col_check:
                    flagged = st.checkbox(
                        "⚠️",
                        key=f"flag_{trip_key}_{rn}_{s['stop_id']}",
                        help=f"標記此站點需要修改 / Flag this stop for changes",
                    )
                with col_info:
                    st.markdown(
                        f"**{idx + 1}. {stop_label}** "
                        f"({stop_students} students, {stop_time})"
                    )
                    if stop_addr:
                        st.caption(f"📍 {stop_addr}")

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

    # Show flagged summary
    if flagged_stops:
        st.warning(
            f"⚠️ 已標記 {len(flagged_stops)} 個站點 / "
            f"{len(flagged_stops)} stops flagged for changes"
        )
        with st.expander("查看標記的站點 / View flagged stops"):
            for fs in flagged_stops:
                st.write(
                    f"- **{fs['label']}** ({fs['trip'].upper()} Route {fs['route']})"
                    + (f" → 建議改到: {fs['suggested_address']}" if fs['suggested_address'] else "")
                )

        if st.button("📝 儲存標記並重新規劃 / Save flags & re-plan", type="secondary"):
            # Save flagged stops to manual_locations.csv for next run
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
                fieldnames = ["stop_id", "address", "source"]
                with open(manual_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(existing)

            st.session_state.phase = "review"  # stay in review, re-plan will run
            st.rerun()

    # Driver start location — ask after stops are known
    st.subheader("🚗 司機起點 / Driver start location")
    driver_start = st.text_input(
        "司機從哪裡開始？路線會從最近的站點開始。 / Where does the driver start? Route begins at nearest stop.",
        placeholder="例如：尖沙咀彌敦道 / e.g. Nathan Road, Tsim Sha Tsui",
        key="driver_start_input",
    )
    if driver_start.strip():
        if st.button("🔄 從此起點重新排序路線 / Re-order route from here", type="secondary"):
            coords = _geocode_address(driver_start.strip())
            if coords:
                dlat, dlng = coords
                # Find nearest stop across all trips
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
                                json.dump(ss, open(start_stops_path, "w", encoding="utf-8"),
                                          ensure_ascii=False)

                with st.spinner(f"🔄 從「{driver_start.strip()}」重新排序… / Reordering…"):
                    pipeline.plan_routes(
                        st.session_state.students_csv, str(SCHOOLS_CSV),
                        capacity=int(bus_capacity), mode=mode,
                    )
                st.toast(f"✅ 路線已從最近站點開始")
                st.session_state.summary = _build_summary(st.session_state.students_csv)
                st.rerun()
            else:
                st.error("❌ 無法識別該地址 / Could not geocode that address")

    st.divider()

    # Generate button
    if st.button("📄 生成 PDF / Generate PDFs", type="primary"):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        st.session_state.output_before = {
            p: p.stat().st_mtime for p in OUTPUT_DIR.iterdir() if p.is_file()
        }

        with st.spinner("⏳ 生成 PDF（約 2–5 分鐘）… / Generating PDFs…"):
            pipeline.generate_outputs(
                st.session_state.students_csv, str(SCHOOLS_CSV),
                capacity=int(bus_capacity), mode=mode,
            )

        st.session_state.phase = "download"
        st.rerun()

# =========================================================================
# STAGE 4: DOWNLOAD
# =========================================================================
if st.session_state.phase == "download":
    st.success("✅ PDF 生成完成 / PDFs ready!")

    pdf_files = sorted(OUTPUT_DIR.glob("*.pdf"))
    html_files = sorted(OUTPUT_DIR.glob("*.html"))
    before = st.session_state.get("output_before", {})
    recent = [
        p for p in (pdf_files + html_files)
        if p.name not in before or p.stat().st_mtime > before[p.name]
    ]

    if recent:
        st.header("📥 下載路線指南 / Download route guides")
        st.caption("請在工作階段結束前下載 — 系統每次運行都會覆寫結果。")
        cols = st.columns(3)
        for i, f in enumerate(sorted(set(recent), key=lambda p: p.name)):
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
            for f in sorted(set(recent), key=lambda p: p.name):
                z.write(f, f.name)
        st.download_button(
            label="📦 下載全部 (ZIP) / Download all (ZIP)",
            data=buf.getvalue(),
            file_name="school-bus-routes.zip",
            mime="application/zip",
        )

    # Manifest table
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
        st.session_state.phase = "upload"
        st.session_state.summary = None
        st.session_state.students_rows = []
        st.session_state.students_csv = None
        st.session_state.output_before = {}
        st.rerun()

st.caption("RRIFA — Re-Route It For All | Admin: see README.md")

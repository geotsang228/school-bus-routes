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
import os
import sys
import zipfile
from pathlib import Path

import streamlit as st

# --- AMap key bootstrap: must happen before `config` is imported ---
try:
    os.environ.setdefault("AMAP_KEY", st.secrets["amap_key"])
except Exception:
    pass

# Make scripts/ importable
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import DATA_DIR, OUTPUT_DIR  # noqa: E402
import normalize_xlsx  # noqa: E402
import pipeline  # noqa: E402

SCHOOLS_CSV = DATA_DIR / "schools.csv"
UPLOADED_CSV = DATA_DIR / "students_uploaded.csv"

# --- Session state defaults ---
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

# Stage pipeline for sidebar display
STAGES = [
    ("upload",     "📤 上載 Excel"),
    ("plan",       "🗺️ 規劃路線"),
    ("review",     "📋 檢視路線"),
    ("generate",   "📄 生成 PDF"),
    ("download",   "📥 下載"),
]


def _fmt_mmss(seconds):
    m, s = int(seconds) // 60, int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def _build_summary(students_csv):
    """Read manifest + stops CSVs and return structured summary for review."""
    summary = {"am": None, "pm": None, "unmatched": [], "total_students": 0}
    students = list(csv.DictReader(open(students_csv, encoding="utf-8-sig")))
    summary["total_students"] = len(students)

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


# =========================================================================
# SIDEBAR — Stage progress + settings
# =========================================================================
st.set_page_config(page_title="RRIFA — 校巴路線規劃", layout="wide")

with st.sidebar:
    st.header("🚌 RRIFA")
    st.caption("Re-Route It For All")

    # Stage progress display
    st.divider()
    current_idx = next(
        (i for i, (key, _) in enumerate(STAGES) if key == st.session_state.phase), 0
    )
    for i, (key, label) in enumerate(STAGES):
        if i < current_idx:
            st.markdown(f"✅ {label}")
        elif i == current_idx:
            st.markdown(f"▶️ **{label}**")
        else:
            st.markdown(f"⬜ {label}")

    st.divider()
    st.header("⚙️ 設定 / Settings")
    bus_capacity = st.selectbox(
        "每車座位 / Seats per bus",
        options=[16, 28, 25],
        index=0,
    )
    mode = st.radio(
        "規劃方式 / Planning mode",
        options=["clustered", "custom"],
        format_func=lambda m: {
            "clustered": "聚類站點 (Clustered)",
            "custom": "每站一人 (Each = own stop)",
        }[m],
    )

# =========================================================================
# MAIN CONTENT
# =========================================================================
st.title("🚌 RRIFA — 校巴路線規劃")
st.caption("Re-Route It For All")

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
    st.caption("學校分佈 / Schools: " + ", ".join(f"{k}: {v}" for k, v in schools_count.items()))

    preview = [
        {"學號 ID": s["student_id"], "姓名 Name": s["name"],
         "學校 School": s["school"], "地址 Address": s["address"]}
        for s in rows
    ]
    st.dataframe(preview, width="stretch", hide_index=True)

# =========================================================================
# STAGE 2: PLAN (with spinner — blocking but simple)
# =========================================================================
can_plan = st.session_state.students_rows and st.session_state.phase in ("upload", "review")

if st.session_state.phase in ("upload", "review"):
    btn_label = "🗺️ 重新規劃 / Re-plan" if st.session_state.phase == "review" else "🗺️ 規劃路線 / Plan routes"
    btn_type = "secondary" if st.session_state.phase == "review" else "primary"

    if st.button(btn_label, type=btn_type, disabled=not st.session_state.students_rows):
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
            elapsed = time.time() - t0

        st.session_state.summary = _build_summary(str(UPLOADED_CSV))
        st.session_state.phase = "review"
        st.rerun()

# =========================================================================
# STAGE 3: REVIEW
# =========================================================================
if st.session_state.phase == "review" and st.session_state.summary:
    summary = st.session_state.summary
    st.success(f"✅ 路線規劃完成 / Routes planned ({summary['total_students']} students)")

    # Unmatched addresses
    unmatched = summary.get("unmatched", [])
    if unmatched:
        st.warning(f"⚠️ {len(unmatched)} 個地址未能識別 / {len(unmatched)} addresses could not be geocoded")
        with st.expander("查看未識別地址 / View unmatched addresses", expanded=True):
            st.dataframe(
                [{"學生 Student": r.get("name", ""), "地址 Address": r.get("address", "")}
                 for r in unmatched],
                width="stretch", hide_index=True,
            )

    # Route summary per trip
    for trip_label, trip_key in [("🌅 AM 早上接送", "am"), ("🌇 PM 下午接送", "pm")]:
        routes = summary.get(trip_key)
        if not routes:
            continue
        st.subheader(trip_label)
        for route in routes:
            rn = route["route_number"]
            n_stops = len(route["stops"])
            n_students = sum(int(s.get("students_at_stop", 0)) for s in route["stops"])
            with st.expander(
                f"🚐 Route {rn} — {route.get('school', '')} "
                f"({n_students} students, {n_stops} stops)"
            ):
                c1, c2, c3 = st.columns(3)
                c1.metric("最快 / Fastest", route.get("fastest_duration", "?"),
                          f"{route.get('fastest_distance_m', '?')}m")
                c2.metric("免路費 / Toll-free", route.get("tollfree_duration", "?"),
                          f"{route.get('tollfree_distance_m', '?')}m")
                c3.metric("學生 / Students", n_students)

                stop_rows = [
                    {"站點 Stop": s["label"], "時間 Time": s["pickup_time"],
                     "學生 Students": s["students_at_stop"]}
                    for s in route["stops"]
                ]
                st.dataframe(stop_rows, width="stretch", hide_index=True)

    # Generate button — only enabled in review phase
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
                st.subheader(f"{'AM 早上接送' if trip == 'am' else 'PM 下午接送'}")
                cols_keep = ["route_number", "stop_id", "pickup_time",
                             "students_at_stop", "fastest_duration",
                             "fastest_distance_m", "fastest_tolls",
                             "tollfree_duration", "tollfree_distance_m", "tollfree_tolls"]
                view = [{c: r.get(c, "") for c in cols_keep} for r in df_rows]
                st.dataframe(view, width="stretch", hide_index=True)

    # Start over
    if st.button("🔄 開始新一輪 / Plan another round"):
        st.session_state.phase = "upload"
        st.session_state.summary = None
        st.session_state.students_rows = []
        st.session_state.students_csv = None
        st.session_state.output_before = {}
        st.rerun()

st.caption("RRIFA — Re-Route It For All | Admin: see README.md")

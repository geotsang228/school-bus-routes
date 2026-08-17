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
import threading
import time
import zipfile
from pathlib import Path

import streamlit as st

# --- AMap key bootstrap: must happen before `config` is imported ---
try:
    os.environ.setdefault("AMAP_KEY", st.secrets["amap_key"])
except Exception:
    pass  # no secrets.toml (local run) — config.py falls back to .env / env var

# Make scripts/ importable and import the existing pipeline pieces as-is.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import DATA_DIR, OUTPUT_DIR  # noqa: E402
import normalize_xlsx  # noqa: E402
import pipeline  # noqa: E402

# --- Session state defaults ---
_defaults = {
    "_PHASE": "upload",          # upload | planning | review | generating | done
    "_RUNNING": False,           # any background worker active
    "_SUMMARY": None,            # route summary dict from plan_routes()
    "_STUDENTS_ROWS": [],        # parsed student rows from uploaded Excel
    "_STUDENTS_CSV": None,       # path to the written CSV
    "_OUTPUT_BEFORE": {},        # snapshot of output/ mtimes before PDF gen
    "_PLAN_TEE": None,           # _Tee for Phase 1 stdout
    "_PDF_TEE": None,            # _Tee for Phase 2 stdout
    "_PLAN_ELAPSED": 0,          # Phase 1 elapsed seconds
    "_PDF_ELAPSED": 0,           # Phase 2 elapsed seconds
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

SCHOOLS_CSV = DATA_DIR / "schools.csv"
UPLOADED_CSV = DATA_DIR / "students_uploaded.csv"

# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

# Phase key -> (progress 0–100, bilingual label)
_PLAN_PHASES = {
    "start":      (0,   "準備中 / Starting"),
    "intake":     (2,   "讀取資料 / Reading data"),
    "geocode":    (12,  "地理編碼 / Geocoding"),
    "am":         (24,  "AM 路線規劃 / AM routing"),
    "cluster_am": (30,  "AM 聚類站點 / AM stops"),
    "route_am":   (48,  "AM 路線計算 / AM routes"),
    "pm":         (60,  "PM 路線規劃 / PM routing"),
    "cluster_pm": (68,  "PM 聚類站點 / PM stops"),
    "route_pm":   (85,  "PM 路線計算 / PM routes"),
    "done":       (100, "路線規劃完成 / Routes planned"),
}

_PDF_PHASES = {
    "start":  (0,   "準備中 / Starting"),
    "pdf_am": (30,  "AM 製作 PDF / AM PDFs"),
    "pdf_pm": (70,  "PM 製作 PDF / PM PDFs"),
    "done":   (100, "PDF 完成 / PDFs done"),
}


def _classify_phase(lines, phase_map):
    phase = "start"
    for line in lines:
        if "[Stage 1]" in line:
            phase = "intake"
        elif "[Stage 2]" in line:
            phase = "geocode"
        elif "TRIP AM" in line:
            phase = "am"
        elif "TRIP PM" in line:
            phase = "pm"
        elif "[Stage 3]" in line:
            phase = "cluster_am" if phase == "am" else "cluster_pm" if phase == "pm" else phase
        elif "[Stage 4]" in line:
            phase = "route_am" if phase in ("am", "cluster_am") else (
                "route_pm" if phase in ("pm", "cluster_pm") else phase)
        elif "[Stage 5]" in line:
            phase = "pdf_am" if "pdf_am" in phase_map else "pdf_pm" if "pdf_pm" in phase_map else phase
            if phase not in phase_map:
                phase = "pdf_am" if phase in ("am", "cluster_am", "route_am") else "pdf_pm"
        elif "Pipeline complete" in line or "complete in" in line:
            phase = "done"
    return phase


def _estimate_eta(history, prog, elapsed):
    if len(history) < 2 or elapsed < 15:
        return None
    oldest_t, oldest_p = history[0]
    span = elapsed - oldest_t
    if span <= 0.5:
        return None
    slope = (prog - oldest_p) / span
    if slope < 0.1:
        return None
    return max(5, (100 - prog) / slope)


def _fmt_mmss(seconds):
    m, s = int(seconds) // 60, int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def _fmt_eta(seconds):
    if seconds > 900:
        return "約 15+ 分鐘 / 15+ min"
    if seconds < 60:
        return f"約 {int(seconds)} 秒 / ~{int(seconds)}s"
    return f"約 {int(seconds // 60) + 1} 分鐘 / ~{int(seconds // 60) + 1} min"


class _Tee:
    """Thread-safe stdout capture."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lines = []
        self._partial = ""
        self.error = None

    def write(self, s):
        if not s:
            return
        with self._lock:
            self._partial += s
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                self._lines.append(line)

    def flush(self):
        pass

    def snapshot(self):
        with self._lock:
            return list(self._lines)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _run_plan_worker(tee, *args, **kwargs):
    old = sys.stdout
    sys.stdout = tee
    try:
        result = pipeline.plan_routes(*args, **kwargs)
        tee.result = result
    except Exception as e:
        tee.error = e
    finally:
        sys.stdout = old


def _run_pdf_worker(tee, *args, **kwargs):
    old = sys.stdout
    sys.stdout = tee
    try:
        pipeline.generate_outputs(*args, **kwargs)
    except Exception as e:
        tee.error = e
    finally:
        sys.stdout = old


def _show_progress(tee, phase_map, start_time, key_prefix):
    """Render a live progress bar + ETA + log from a running worker thread."""
    history = []
    display_prog = 0.0
    placeholder = st.empty()
    with placeholder.container():
        bar = st.progress(0.0)
        c1, c2 = st.columns(2)
        mel, met = c1.empty(), c2.empty()
        cap = st.empty()
        with st.expander("詳細進度 / Detailed log", expanded=False):
            logbox = st.empty()

        while st.session_state["_RUNNING"]:
            lines = tee.snapshot()
            phase = _classify_phase(lines, phase_map)
            prog, label = phase_map.get(phase, (0, "準備中 / Starting"))
            el = time.monotonic() - start_time
            cutoff = el - 25.0
            history = [(t, p) for t, p in history + [(el, prog)] if t >= cutoff]
            eta = _estimate_eta(history, prog, el)
            display_prog += (prog - display_prog) * 0.25

            bar.progress(min(1.0, max(0.0, display_prog / 100.0)))
            mel.metric("已用時間 / Elapsed", _fmt_mmss(el))
            met.metric("預計剩餘 / ETA",
                       _fmt_eta(eta) if eta else "仍在計算… / working")
            cap.caption(label)
            logbox.code("\n".join(lines[-40:]), language="text")
            time.sleep(0.5)

    return time.monotonic() - start_time


# ---------------------------------------------------------------------------
# Page config + layout
# ---------------------------------------------------------------------------
st.set_page_config(page_title="RRIFA — 校巴路線規劃", layout="wide")

with st.sidebar:
    st.header("⚙️ 設定 / Settings")
    bus_capacity = st.selectbox(
        "每車座位 / Seats per bus",
        options=[16, 28, 25],
        index=0,
        help="16 或 28 座（常用）。25 為系統預設。",
    )
    mode = st.radio(
        "規劃方式 / Planning mode",
        options=["clustered", "custom"],
        format_func=lambda m: {
            "clustered": "聚類站點 (Clustered stops)",
            "custom": "每個學生一站 (Each student = own stop)",
        }[m],
    )

st.title("🚌 RRIFA — 校巴路線規劃")
st.caption("Re-Route It For All")
st.markdown(
    "上載學生名單 Excel → 預覽路線 → 確認後生成 PDF。\n\n"
    "Upload student list → preview routes → confirm to generate PDFs."
)

# =========================================================================
# PHASE 0: UPLOAD
# =========================================================================
uploaded = st.file_uploader(
    "上載學生名單 Excel / Upload student list (.xlsx)",
    type=["xlsx"],
)

if uploaded is not None and not st.session_state["_STUDENTS_ROWS"]:
    tmp = DATA_DIR / "uploaded.xlsx"
    tmp.write_bytes(uploaded.getvalue())
    try:
        header, raw_rows = normalize_xlsx.read_xlsx(tmp)
        rows = [
            normalize_xlsx.to_pipeline_row(r, header) for r in raw_rows
            if normalize_xlsx.to_pipeline_row(r, header)["student_id"]
        ]
        st.session_state["_STUDENTS_ROWS"] = rows
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
# PHASE 1: PLAN ROUTINES (Stages 1–4)
# =========================================================================
can_plan = (st.session_state["_STUDENTS_ROWS"] and
            st.session_state["_PHASE"] in ("upload", "review"))

if st.session_state["_PHASE"] == "upload" and st.session_state["_STUDENTS_ROWS"]:
    if st.button("🗺️ 規劃路線 / Plan routes", type="primary"):
        # Write CSV
        fieldnames = ["student_id", "name", "name_en", "school", "class_year",
                      "address", "dropoff_address", "district",
                      "contact_phone", "contact_name"]
        with open(UPLOADED_CSV, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(st.session_state["_STUDENTS_ROWS"])
        st.session_state["_STUDENTS_CSV"] = str(UPLOADED_CSV)
        st.session_state["_PHASE"] = "planning"
        st.session_state["_RUNNING"] = True
        st.rerun()

if st.session_state["_PHASE"] == "planning":
    st.info("⏳ 路線規劃中（約 1–3 分鐘）… / Planning routes (~1–3 min)…")
    tee = _Tee()
    st.session_state["_PLAN_TEE"] = tee
    worker = threading.Thread(
        target=_run_plan_worker,
        args=(tee, st.session_state["_STUDENTS_CSV"], str(SCHOOLS_CSV)),
        kwargs={"capacity": int(bus_capacity), "mode": mode},
        daemon=True,
    )
    t_start = time.monotonic()
    worker.start()
    elapsed = _show_progress(tee, _PLAN_PHASES, t_start, "plan")
    worker.join()

    st.session_state["_RUNNING"] = False
    st.session_state["_PLAN_ELAPSED"] = elapsed

    if tee.error:
        st.error(f"規劃失敗 / Planning failed: {tee.error}")
        st.code("\n".join(tee.snapshot()[-60:]), language="text")
        st.session_state["_PHASE"] = "upload"
    else:
        st.session_state["_SUMMARY"] = tee.result
        st.session_state["_PHASE"] = "review"
    st.rerun()

# =========================================================================
# PHASE 1.5: REVIEW ROUTES
# =========================================================================
if st.session_state["_PHASE"] == "review" and st.session_state["_SUMMARY"]:
    summary = st.session_state["_SUMMARY"]
    st.success(f"✅ 路線規劃完成（{_fmt_mmss(summary.get('_elapsed', st.session_state['_PLAN_ELAPSED']))}）"
               if "_elapsed" in summary else
               f"✅ 路線規劃完成 / Routes planned in {_fmt_mmss(st.session_state['_PLAN_ELAPSED'])}")

    # Unmatched addresses — show prominently
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
            fastest_t = route.get("fastest_duration", "?")
            fastest_d = route.get("fastest_distance_m", "?")
            tollfree_t = route.get("tollfree_duration", "?")
            tollfree_d = route.get("tollfree_distance_m", "?")

            with st.expander(
                f"🚐 Route {rn} — {route.get('school', '')} "
                f"({n_students} students, {n_stops} stops)"
            ):
                c1, c2, c3 = st.columns(3)
                c1.metric("最快 / Fastest", fastest_t, f"{fastest_d}m")
                c2.metric("免路費 / Toll-free", tollfree_t, f"{fastest_d}m")
                c3.metric("學生 / Students", n_students)

                stop_rows = [
                    {"站點 Stop": s["label"], "時間 Time": s["pickup_time"],
                     "學生 Students": s["students_at_stop"]}
                    for s in route["stops"]
                ]
                st.dataframe(stop_rows, width="stretch", hide_index=True)

    # Action buttons
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✏️ 重新規劃 / Re-plan (change settings)", type="secondary"):
            st.session_state["_PHASE"] = "upload"
            st.session_state["_SUMMARY"] = None
            st.rerun()
    with c2:
        if st.button("📄 生成 PDF / Generate PDFs", type="primary"):
            st.session_state["_PHASE"] = "generating"
            st.session_state["_RUNNING"] = True
            st.rerun()

# =========================================================================
# PHASE 2: GENERATE PDFs (Stage 5)
# =========================================================================
if st.session_state["_PHASE"] == "generating":
    st.info("⏳ 生成 PDF（約 2–5 分鐘）… / Generating PDFs (~2–5 min)…")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    st.session_state["_OUTPUT_BEFORE"] = {
        p: p.stat().st_mtime for p in OUTPUT_DIR.iterdir() if p.is_file()
    }

    tee = _Tee()
    st.session_state["_PDF_TEE"] = tee
    worker = threading.Thread(
        target=_run_pdf_worker,
        args=(tee, st.session_state["_STUDENTS_CSV"], str(SCHOOLS_CSV)),
        kwargs={"capacity": int(bus_capacity), "mode": mode},
        daemon=True,
    )
    t_start = time.monotonic()
    worker.start()
    elapsed = _show_progress(tee, _PDF_PHASES, t_start, "pdf")
    worker.join()

    st.session_state["_RUNNING"] = False
    st.session_state["_PDF_ELAPSED"] = elapsed

    if tee.error:
        st.error(f"PDF 生成失敗 / PDF generation failed: {tee.error}")
        st.code("\n".join(tee.snapshot()[-60:]), language="text")
        st.session_state["_PHASE"] = "review"
    else:
        st.session_state["_PHASE"] = "done"
    st.rerun()

# =========================================================================
# PHASE 3: DOWNLOAD
# =========================================================================
if st.session_state["_PHASE"] == "done":
    st.success(f"✅ PDF 生成完成（{_fmt_mmss(st.session_state['_PDF_ELAPSED'])}）")

    pdf_files = sorted(OUTPUT_DIR.glob("*.pdf"))
    html_files = sorted(OUTPUT_DIR.glob("*.html"))
    before = st.session_state.get("_OUTPUT_BEFORE", {})
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

        # ZIP download
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

    # Route manifest table
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
        for k in ("_PHASE", "_RUNNING", "_SUMMARY", "_STUDENTS_ROWS",
                   "_STUDENTS_CSV", "_OUTPUT_BEFORE"):
            st.session_state[k] = _defaults[k]
        st.rerun()

# =========================================================================
# Footer
# =========================================================================
st.caption("RRIFA — Re-Route It For All | Admin: see README.md")

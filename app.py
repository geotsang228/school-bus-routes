"""
School Bus Routes — Streamlit web app for non-technical colleagues.

Usage (local):  streamlit run app.py
Usage (cloud):  deploy on Streamlit Community Cloud — see README.md

Flow:  upload Excel student list → preview → run pipeline → download PDFs.
Bilingual Traditional Chinese / English.
"""
import io
import os
import sys
import csv
import threading
import time
import zipfile
from pathlib import Path

import streamlit as st

# --- AMap key bootstrap: must happen before `config` is imported ---
# On Streamlit Cloud the key lives in Streamlit Secrets; locally it can
# also be set via env var or scripts/.env (config.py falls back to those).
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

# Single-process concurrency guard: AMap free-tier throttles bursts, and two
# colleagues clicking Run at once would double-fire the pipeline.
if "_RUNNING" not in st.session_state:
    st.session_state["_RUNNING"] = False
# Set True after a successful pipeline run in THIS session, so stale PDFs
# from an earlier run/colleague are never shown as this user's results.
if "_HAS_RUN" not in st.session_state:
    st.session_state["_HAS_RUN"] = False

SCHOOLS_CSV = DATA_DIR / "schools.csv"
UPLOADED_CSV = DATA_DIR / "students_uploaded.csv"

# ---------------------------------------------------------------------------
# Live progress — run the pipeline in a worker thread, tee its stdout, and
# estimate remaining time from how fast it advances through the pipeline
# stages. The pipeline prints stable stage markers, so we track phases.
# ---------------------------------------------------------------------------

# Phase key -> (progress 0-100, bilingual label shown to the colleague)
_PHASE_META = {
    "start":      (0,   "準備中 / Starting"),
    "intake":     (2,   "讀取資料 / Reading data"),
    "geocode":    (12,  "地理編碼（最花時間的一步）/ Geocoding (the slow step)"),
    "am":         (24,  "早上接送開始 / AM trip started"),
    "cluster_am": (30,  "AM 聚類站點 / AM grouping stops"),
    "route_am":   (48,  "AM 路線規劃 / AM planning routes"),
    "pdf_am":     (60,  "AM 製作 PDF / AM making PDFs"),
    "pm":         (64,  "下午接送開始 / PM trip started"),
    "cluster_pm": (70,  "PM 聚類站點 / PM grouping stops"),
    "route_pm":   (85,  "PM 路線規劃 / PM planning routes"),
    "pdf_pm":     (95,  "PM 製作 PDF / PM making PDFs"),
    "done":       (100, "完成 / Done"),
}


def _classify_phase(lines):
    """Map the collected pipeline log lines to the current phase key."""
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
            phase = "pdf_am" if phase in ("am", "cluster_am", "route_am") else (
                "pdf_pm" if phase in ("pm", "cluster_pm", "route_pm") else phase)
        elif "Pipeline complete" in line:
            phase = "done"
    return phase


def _estimate_eta(history, prog, elapsed):
    """Seconds-to-go from recent progress rate; None if not enough data yet."""
    if len(history) < 2 or elapsed < 30:
        return None
    oldest_t, oldest_p = history[0]
    span = elapsed - oldest_t
    if span <= 0.5:
        return None
    slope = (prog - oldest_p) / span          # progress points per second
    if slope < 0.1:
        return None                            # stuck on a long API call
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
    """Thread-safe stdout capture: buffers whole lines, safe to snapshot."""

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


def _run_pipeline_worker(tee, *args, **kwargs):
    old = sys.stdout
    sys.stdout = tee
    try:
        pipeline.run_pipeline(*args, **kwargs)
    except Exception as e:
        tee.error = e
    finally:
        sys.stdout = old

st.set_page_config(page_title="校巴路線規劃 / School Bus Route Planner", layout="wide")

# --- Sidebar: admin controls (kept out of the main colleague flow) ---
with st.sidebar:
    st.header("⚙️ 設定 / Settings")
    bus_capacity = st.selectbox(
        "每車座位 / Seats per bus",
        options=[16, 28, 25],
        index=0,
        help="16 或 28 座（常用）。25 為系統預設。 / 16 or 28 seats (common). 25 = system default.",
    )
    mode = st.radio(
        "規劃方式 / Planning mode",
        options=["clustered", "custom"],
        format_func=lambda m: {
            "clustered": "聚類站點 (Clustered stops)",
            "custom": "每個學生一站 (Each student = own stop)",
        }[m],
        help="預設為聚類站點，與歷年做法一致。 / Default clustered stops, matching past years.",
    )
    st.caption("管理員設定 / Admin settings")

st.title("🚌 校巴路線規劃 / School Bus Route Planner")
st.markdown(
    "上載學生名單 Excel → 按「規劃路線」→ 下載路線 PDF。\n\n"
    "Upload the student list Excel → click **Plan routes** → download the route PDFs.\n\n"
    "*路程需時約 3–10 分鐘，請耐心等候。 / Takes ~3–10 minutes — please wait.*"
)

# ---------------------------------------------------------------------------
# 1. Upload
# ---------------------------------------------------------------------------
uploaded = st.file_uploader(
    "上載學生名單 Excel / Upload student list (.xlsx)",
    type=["xlsx"],
)

students_rows = []
if uploaded is not None:
    # Save the upload to a temp file so openpyxl can read it
    tmp = DATA_DIR / "uploaded.xlsx"
    tmp.write_bytes(uploaded.getvalue())
    try:
        header, raw_rows = normalize_xlsx.read_xlsx(tmp)
        students_rows = [
            normalize_xlsx.to_pipeline_row(r, header) for r in raw_rows
            if normalize_xlsx.to_pipeline_row(r, header)["student_id"]
        ]
    except Exception as e:
        st.error(f"讀取 Excel 失敗 / Failed to read Excel: {e}")
        st.stop()

    if not students_rows:
        st.warning("Excel 內沒有有效學生紀錄 / No valid student records found in the Excel.")
        st.stop()

    schools_count = {}
    for s in students_rows:
        schools_count[s["school"] or "（未填學校 / no school）"] = (
            schools_count.get(s["school"] or "（未填學校 / no school）", 0) + 1
        )

    st.success(
        f"讀取 {len(students_rows)} 條學生紀錄 / Loaded {len(students_rows)} student records."
    )
    st.caption("學校分佈 / Schools: " + ", ".join(f"{k}: {v}" for k, v in schools_count.items()))

    # Show a preview so the colleague can sanity-check before the slow run
    preview = [
        {"學號 ID": s["student_id"], "姓名 Name": s["name"],
         "學校 School": s["school"], "地址 Address": s["address"]}
        for s in students_rows
    ]
    st.dataframe(preview, width="stretch", hide_index=True)

# ---------------------------------------------------------------------------
# 2. Run
# ---------------------------------------------------------------------------
run_disabled = (uploaded is None) or st.session_state["_RUNNING"]
if st.session_state["_RUNNING"]:
    st.info("⏳ 路線規劃進行中，請稍候… / Route planning in progress, please wait…")

if st.button("🚦 規劃路線 / Plan routes", disabled=run_disabled, type="primary"):
    # Write the normalized CSV the pipeline expects
    fieldnames = ["student_id", "name", "name_en", "school", "class_year",
                  "address", "dropoff_address", "district",
                  "contact_phone", "contact_name"]
    with open(UPLOADED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(students_rows)

    st.session_state["_RUNNING"] = True
    # Output folder may not exist on a fresh deploy (git doesn't ship empty
    # dirs and output/ is gitignored) — create it before snapshotting.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Snapshot pre-run output mtimes so the results panel shows exactly the
    # files this run wrote/overwrote (not stale files from an earlier run).
    st.session_state["_OUTPUT_BEFORE"] = {
        p: p.stat().st_mtime for p in OUTPUT_DIR.iterdir() if p.is_file()
    }

    # Run the pipeline in a background thread so the UI can update live.
    tee = _Tee()
    worker = threading.Thread(
        target=_run_pipeline_worker,
        args=(tee, str(UPLOADED_CSV), str(SCHOOLS_CSV)),
        kwargs={"capacity": int(bus_capacity), "mode": mode},
        daemon=True,
    )
    t_start = time.monotonic()
    worker.start()

    history = []
    display_prog = 0.0
    try:
        with st.status("規劃路線中… / Planning routes…", expanded=True) as status:
            bar = st.progress(0.0)
            c1, c2 = st.columns(2)
            mel, met = c1.empty(), c2.empty()
            cap = st.empty()
            with st.expander("詳細進度 / Detailed log", expanded=False):
                logbox = st.empty()

            while worker.is_alive():
                lines = tee.snapshot()
                phase = _classify_phase(lines)
                prog, label = _PHASE_META.get(phase, (0, "準備中 / Starting"))
                el = time.monotonic() - t_start
                cutoff = el - 25.0
                history = [(t, p) for t, p in history + [(el, prog)] if t >= cutoff]
                eta = _estimate_eta(history, prog, el)
                display_prog += (prog - display_prog) * 0.25  # glide the bar

                bar.progress(min(1.0, max(0.0, display_prog / 100.0)))
                mel.metric("已用時間 / Elapsed", _fmt_mmss(el))
                met.metric("預計剩餘 / ETA",
                           _fmt_eta(eta) if eta else "仍在計算… / working")
                cap.caption(label)
                logbox.code("\n".join(lines[-60:]), language="text")
                time.sleep(0.5)

            worker.join()
            if tee.error:
                raise tee.error
            status.update(label="完成 / Done！", state="complete", expanded=False)
    except Exception as e:
        st.session_state["_RUNNING"] = False
        st.error(f"規劃失敗 / Planning failed: {e}")
        st.code("\n".join(tee.snapshot()[-60:]), language="text")
        st.stop()
    finally:
        st.session_state["_RUNNING"] = False
        st.session_state["_HAS_RUN"] = True
    st.rerun()

# ---------------------------------------------------------------------------
# 3. Results
# ---------------------------------------------------------------------------
pdf_files = sorted(OUTPUT_DIR.glob("*.pdf"))
html_files = sorted(OUTPUT_DIR.glob("*.html"))

if st.session_state["_HAS_RUN"] and (pdf_files or html_files):
    st.divider()
    st.header("📥 下載路線指南 / Download route guides")
    st.caption("請在工作階段結束前下載 — 系統每次運行都會覆寫結果。 / Download before you leave the page — results are overwritten each run.")
    # Only files touched by this session's run (created new, or mtime changed
    # vs the pre-run snapshot). Stale outputs are never shown as results.
    before = st.session_state.get("_OUTPUT_BEFORE", {})
    recent = [
        p for p in (pdf_files + html_files)
        if p.name not in before or p.stat().st_mtime > before[p.name]
    ]

    if recent:
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

    # One-click zip of every PDF/HTML from this run
    if recent:
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

# ---------------------------------------------------------------------------
# 4. Manifest review (inline, no download needed)
# ---------------------------------------------------------------------------
if st.session_state["_HAS_RUN"]:
    st.divider()
    st.header("📋 路線總覽 / Route manifest overview")
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

st.caption(
    "管理員設置 / Admin setup: see README.md. AMap key 配置於 Streamlit Secrets。"
)
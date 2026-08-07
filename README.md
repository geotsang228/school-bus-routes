# 校巴路線規劃系統 / School Bus Route Planner

Bilingual (繁體中文 / English) school-bus route planner, now with a **web app** so non-technical colleagues can plan routes themselves: upload the student list Excel, click one button, download the route PDFs. Runs on **Streamlit Community Cloud** (free).

Local CLI pipeline unchanged — see `CLAUDE.md` for the full pipeline documentation.

---

## 🖥️ For colleagues (using the app)

1. Open the shared URL (your admin will send it).
2. **Upload student list Excel** (`.xlsx`) — the school's usual format (`學號 / 姓名 / School 學校 / 住址 Address`).
3. Pick the **seats per bus** in the sidebar (16 or 28 — your fleet; 25 is the system default).
4. Preview shows how many students were read and per-school counts — sanity-check it.
5. Click **🚦 規劃路線 / Plan routes**. Takes ~3–10 minutes. The system auto-groups students into routes, each capped at the seat count you picked (one bus per route). A live panel shows the **elapsed time, estimated time remaining, and current stage** so you know it's still working.
6. Download the route guides:
   - **📦 下載全部 (ZIP)** — everything in one file (recommended), or
   - Individual **⬇️ PDF** per route (AM morning / PM afternoon).
7. **Download before leaving the page** — results are overwritten by the next run.

No installation, no login beyond what your admin gives you. PC or Mac, any browser.

---

## 🔧 For the admin (one-time setup)

### A. Push the code to GitHub (public repo on the free tier)

Create a public repo (e.g. `school-bus-routes`) and push these — **nothing else**:

```
app.py
requirements.txt
.streamlit/
README.md
scripts/           (pipeline code)
data/schools.csv   (school list — needed; real student CSVs must NOT be pushed)
```

`.gitignore` already excludes `scripts/.env`, `data/students_*.csv`, `output/`, `.venv/`. **Never commit `scripts/.env`** — it contains the AMap key. Real student CSVs contain PII and must never be pushed.

### B. Deploy on Streamlit Community Cloud

1. Go to **share.streamlit.io** → sign in with GitHub → **Create app** → paste the repo URL → set **Main file** to `app.py`.
2. **Deploy.** On first run it installs `requirements.txt` (takes a few minutes).
3. **Add the AMap secret**: Settings → Secrets → add:
   ```toml
   amap_key = "粘贴你的高德 key / paste your AMap key here"
   ```
   The key is the same free 個人開發者 key already in your local `scripts/.env`. It stays hidden from users — they never see it.
4. **Apt packages (for WeasyPrint PDFs)**: Settings → Advanced settings → Apt packages:
   ```
   libpango-1.0-0 libpangoft2-1.0-0 libcairo2
   ```
   These native libs are required by WeasyPrint on Linux. Without them the PDFs won't render (only the HTML fallback shows).
5. **Redeploy** (Deploy again) so the secret + apt settings take effect.
6. Share the app URL with colleagues.

### C. Each year / each new student list

Colleagues use the app directly — no admin action needed. For your own archive, keep running the pipeline locally as before.

---

## 🧪 Testing the app locally (optional)

```bash
cd "School Bus Routes"
.venv/Scripts/python.exe -m pip install streamlit weasyprint openpyxl
AMAP_KEY=<your-key> .venv/Scripts/python.exe -m streamlit run app.py
```

Open the printed URL, upload `data/students_2026_sample.xlsx`, click Plan routes, and confirm the PDFs appear. On Windows WeasyPrint may warn about missing fonts — the renderer falls back to headless Edge, which is fine.

---

## ⚠️ Notes & limits

- **AMap throttling**: the free tier throttles rapid bursts. The app has a run-lock so two colleagues can't trigger the pipeline at the same time — a second click waits / shows "規劃進行中".
- **Cloud storage is ephemeral**: results live in the app container and are lost on redeploy. Always download outputs before leaving the page. For year-to-year records, Geoffrey keeps the canonical local copies in `output/`.
- **Public repo**: code is public on the free GitHub plan. No PII and no keys are in it — verify before pushing.

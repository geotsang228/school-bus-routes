
"""
School Bus Routes — PDF guide generator (Traditional Chinese, 3-page layout).
Page 1: 路線總覽 + 學生名單 + 聯絡資料
Page 2: 站點摘要
Page 3: 路線地圖 + 行車指引
PDF renderers: WeasyPrint → headless Edge (Windows) → keep HTML.
"""
import csv, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import OUTPUT_DIR, DATA_DIR

# Simplified → Traditional Chinese (for AMap stop names / roads)
try:
    from opencc import OpenCC
    _CC = OpenCC("s2t")
    def _tc(s):
        return _CC.convert(s) if s else s
except ImportError:
    def _tc(s):
        return s

def _clean_stop(s):
    """Traditional Chinese stop name, with 公交站-type suffixes removed
    (keep 巴士站 / 巴士總站), and 總站 shortened to 站."""
    s = _tc(s or "")
    for suf in ("（公交站）", "(公交站)", "公交站", "（公交車站）", "公共汽车站"):
        s = s.replace(suf, "")
    s = s.replace("總站", "站").replace("总站", "站")
    return s.strip()

# ---------------------------------------------------------------------------
# Template — Traditional Chinese only, 3-page layout
# ---------------------------------------------------------------------------
TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<style>
  @page { size: A4; margin: 16mm 14mm 14mm 14mm; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: "Noto Sans CJK TC", "Microsoft YaHei", "PingFang TC", sans-serif;
         font-size: 11pt; color: #111; line-height: 1.45; }
  .pg { page-break-before: always; }
  .pg:first-child { page-break-before: auto; }

  .header { display: flex; justify-content: space-between; align-items: flex-end;
            border-bottom: 3px solid #004488; padding-bottom: 6px; margin-bottom: 10px; }
  .header h1 { font-size: 20pt; color: #004488; }
  .header .meta { font-size: 9pt; text-align: right; color: #555; }
  .header .meta b { color: #004488; }

  .variants { display: flex; gap: 8px; margin: 4px 0 10px; }
  .variant { flex: 1; border: 2px solid #004488; border-radius: 6px; padding: 6px 10px; }
  .variant.alt { border-color: #888; }
  .variant-title { font-size: 11pt; font-weight: bold; color: #004488; margin-bottom: 1px; }
  .variant.alt .variant-title { color: #555; }
  .variant .summary { font-size: 12pt; font-weight: bold; }
  .variant .detail { font-size: 8.5pt; color: #555; }

  table { width: 100%; border-collapse: collapse; margin-bottom: 10px; }
  th { background: #004488; color: white; text-align: left; padding: 5px 6px;
       font-size: 9pt; white-space: nowrap; }
  td { padding: 4px 6px; border-bottom: 1px solid #ddd; font-size: 9pt; }
  tr:nth-child(even) td { background: #f4f7fb; }
  .stopdesc { font-size: 8pt; color: #888; margin-top: 1px; }
  .students { font-size: 8.5pt; color: #333; margin-top: 1px; }

  h2.section { font-size: 12pt; color: #004488; border-left: 4px solid #004488;
               padding-left: 8px; margin: 14px 0 6px; }
  ol.instructions { margin: 0 0 10px; padding-left: 22px; }
  ol.instructions li { margin-bottom: 3px; font-size: 10pt; }
  ol.instructions li.road { font-size: 9pt; color: #555; margin-bottom: 1px; }

  .map-box { border: 1px solid #ccc; border-radius: 4px; overflow: hidden;
             margin: 8px 0 10px; background: #e8eef5; }
  .map-box img { width: 100%; height: auto; display: block; }
  .map-note { font-size: 9pt; color: #004488; font-weight: bold; margin: 6px 0 2px; }

  .leg { page-break-inside: avoid; margin-bottom: 26px; }
  .leg-maps { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 4px; }
  .leg-map-box { flex: 1; min-width: 0; }
  .leg-map-box .map-note { font-size: 8.5pt; color: #004488; font-weight: bold; margin: 4px 0 2px; }
  .leg-map-box img { width: 100%; height: auto; border: 1px solid #ccc;
                     border-radius: 4px; display: block; }
  .leg-text { margin-top: 4px; }
  .leg-text b { font-size: 10.5pt; color: #004488; }
  .leg-roads { font-size: 9pt; color: #333; margin-top: 1px; }
  .leg-meta { font-size: 9pt; color: #555; }

  .footer { font-size: 8pt; color: #666; border-top: 1px solid #ccc;
            padding-top: 5px; margin-top: 12px; display: flex; justify-content: space-between; }
</style>
</head>
<body>
<!-- ===== PAGE 1: 路線總覽 + 學生名單 ===== -->
<div>
  <div class="header">
    <div>
      <h1>__ROUTE_LABEL__</h1>
      <div style="font-size:11pt;color:#333;font-weight:bold">__SCHOOL_NAME_LINE__</div>
      <div style="font-size:8.5pt;color:#777">__SCHOOL_ADDR__</div>
    </div>
    <div class="meta">
      <div><b>司機:</b> _______________</div>
      <div><b>車長:</b> _______________</div>
      <div><b>車牌:</b> _______________</div>
    </div>
  </div>

  <div class="variants">
    <div class="variant">
      <div class="variant-title">最快路線</div>
      <div class="summary">__FASTEST_SUMMARY__</div>
      <div class="detail">__FASTEST_DETAIL__</div>
    </div>
    <div class="variant alt">
      <div class="variant-title">最短路線</div>
      <div class="summary">__SHORTEST_SUMMARY__</div>
      <div class="detail">__SHORTEST_DETAIL__</div>
    </div>
    <div class="variant alt">
      <div class="variant-title">免路費路線</div>
      <div class="summary">__TOLLFREE_SUMMARY__</div>
      <div class="detail">__TOLLFREE_DETAIL__</div>
    </div>
  </div>

  <h2 class="section">學生名單及聯絡資料</h2>
  <table>
    <tr><th>#</th><th>姓名</th><th>班級</th><th>地址</th><th>電話</th><th>聯絡人</th><th>__STOP_COL_HEADER__</th></tr>
    __ROSTER_ROWS__
  </table>
</div>

<!-- ===== PAGE 2: 站點摘要 ===== -->
<div class="pg">
  <h2 class="section">__TRIP_TITLE__ 站點摘要</h2>
  <table>
    <tr>
      <th>站號</th><th>站點</th><th>__TIME_HEADER__</th><th>學生人數</th><th>學生姓名</th>
    </tr>
    __STOP_ROWS__
  </table>
</div>

<!-- ===== PAGE 3: 路線地圖 ===== -->
<div class="pg">
  <h2 class="section">路線地圖</h2>
  __ROUTE_MAP__
</div>

<!-- ===== PAGE 4: 行車指引 ===== -->
<div class="pg">
  <h2 class="section">行車指引</h2>
  __DRIVING_INSTRUCTIONS__

  <div class="footer">
    <div><b>緊急聯絡:</b> _______________________ &nbsp;&nbsp; <b>電話:</b> _____________</div>
    <div>校車路線 &copy; __YEAR__</div>
  </div>
</div>
</body>
</html>
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _student_names_for_stop(students_csv, stop_id):
    names = []
    with open(students_csv, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("stop_id") == stop_id:
                names.append(row.get("name", row.get("name_en", "")))
    return names


def _route_diagram_svg(stop_rows):
    """Minimalist schematic route (fallback when no real map)."""
    n = len(stop_rows)
    W, H = 750, 230
    y_track = 110; school_x = 690
    xs = [90 + i * ((660 - 90) / max(n - 1, 1)) for i in range(n)] if n else [375]
    parts = [
        f'<line x1="70" y1="{y_track}" x2="{school_x - 30}" y2="{y_track}" '
        f'stroke="#004488" stroke-width="3"/>',
        f'<polygon points="{school_x - 22},{y_track - 8} {school_x - 22},{y_track + 8} '
        f'{school_x - 6},{y_track}" fill="#004488"/>',
    ]
    for i, (x, row) in enumerate(zip(xs, stop_rows)):
        num = row.get("route_order", i + 1)
        parts += [
            f'<circle cx="{x:.0f}" cy="{y_track}" r="17" fill="#004488"/>',
            f'<text x="{x:.0f}" y="{y_track + 6}" fill="white" text-anchor="middle" '
            f'font-size="13" font-weight="bold">{num}</text>',
            f'<text x="{x:.0f}" y="{y_track + 40}" fill="#004488" text-anchor="middle" '
            f'font-size="11">{row["stop_id"]}</text>',
            f'<text x="{x:.0f}" y="{y_track + 54}" fill="#555" text-anchor="middle" '
            f'font-size="11">{row.get("pickup_time","")}</text>',
        ]
    parts += [
        f'<circle cx="{school_x}" cy="{y_track}" r="21" fill="#DD0000"/>',
        f'<text x="{school_x}" y="{y_track + 6}" fill="white" text-anchor="middle" '
        f'font-size="13" font-weight="bold">校</text>',
        f'<text x="{school_x}" y="{y_track + 42}" fill="#DD0000" text-anchor="middle" '
        f'font-size="11">學校</text>',
    ]
    return (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;height:auto;background:#f4f7fb;border:1px solid #ccc;'
            f'border-radius:4px;margin:8px 0;display:block">'
            + "".join(parts) + "</svg>")


def _fmt_route(dist_m, tolls):
    try: d = f"{int(dist_m) / 1000:.1f} km"
    except (TypeError, ValueError): d = "—"
    t = f"¥{tolls}" if tolls not in ("", None, "—") else "¥0"
    return f"{d} · {t}"


def _concise_instructions(steps, route_rows, trip, school, stop_names):
    """Stop-by-stop driver guide with key roads per leg."""
    if not steps or not route_rows:
        return ""
    n = len(route_rows)

    def road_html(leg):
        roads, seen = [], set()
        for st in leg:
            r = (st.get("road") or "").strip()
            if r and st.get("distance_m", 0) >= 250 and r not in seen:
                seen.add(r); roads.append(r)
            if len(roads) >= 2: break
        dist = sum(st.get("distance_m", 0) for st in leg)
        note = f" ({dist/1000:.1f} km)" if dist >= 500 else ""
        return f'<li class="road">→ {" → ".join(roads)}{note}</li>' if roads else ""

    def stop_li(idx):
        row = route_rows[idx]
        sname = _tc(stop_names.get(row["stop_id"], ("", "", ""))[0])
        action = "上車" if trip == "am" else "下車"
        label = sname or row["stop_id"]
        return f'<li><b>{idx+1}. {label}</b>　{action} {row.get("pickup_time","")}</li>'

    arrivals = [i for i, st in enumerate(steps) if "到達" in st.get("instruction", "") or "到达" in st.get("instruction", "")]
    lines = [stop_li(0)] if trip == "am" else [f'<li class="road">🏫 出發 {school}</li>']
    prev = 0
    for k, ai in enumerate(arrivals):
        leg = steps[prev:ai]; prev = ai
        if trip == "pm":
            if k < n: lines += [road_html(leg), stop_li(k)]
        else:
            if k == len(arrivals) - 1:
                lines += [road_html(leg), f'<li class="road">🏫 抵達 {school}</li>']
            elif k + 1 < n:
                lines += [road_html(leg), stop_li(k + 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build PDF HTML
# ---------------------------------------------------------------------------

def build_route_pdf(route_rows, route_label, school, students_csv,
                    stop_names=None, school_pt=None, polyline="",
                    instructions=None, legs=None, trip="am", year="2026",
                    school_cn="", school_addr="", stop_labels=None):
    stop_names = stop_names or {}
    r0 = route_rows[0]
    trip_label = "接載" if trip == "am" else "放學"
    time_header = "上車時間" if trip == "am" else "下車時間"
    time_label = "上車站" if trip == "am" else "下車站"
    school_name_line = school if not school_cn else f"{school}　{school_cn}"

    # --- Roster (page 1) ---
    student_set = set(row["stop_id"] for row in route_rows)
    students = []
    try:
        with open(students_csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("stop_id") in student_set:
                    students.append(row)
    except FileNotFoundError:
        pass

    roster_trs = []
    for i, s in enumerate(students, 1):
        sid = s.get("stop_id", "")
        sname, _, _ = stop_names.get(sid, ("", "", ""))
        roster_trs.append(
            f'<tr>'
            f'<td>{i}</td>'
            f'<td>{s.get("name","")}</td>'
            f'<td>{s.get("class_year","")}</td>'
            f'<td>{s.get("address","")}</td>'
            f'<td>{s.get("contact_phone","")}</td>'
            f'<td>{s.get("contact_name","")}</td>'
            f'<td>{sname}</td>'
            f'</tr>'
        )

    # --- Stop summary (page 2) with student names ---
    stop_labels = stop_labels or {}
    stop_trs = []
    for row in route_rows:
        sname, sdesc, sstreet = stop_names.get(row["stop_id"], (row["stop_id"], "", ""))
        slabel = (stop_labels.get(row["stop_id"]) or "").strip() or sname or row["stop_id"]
        desc_html = f'<div class="stopdesc">{sdesc}</div>' if sdesc else ""
        names = _student_names_for_stop(students_csv, row["stop_id"])
        names_html = ", ".join(names[:10])
        if len(names) > 10:
            names_html += f" +{len(names)-10}"
        names_td = f'<td>{names_html}</td>' if names else "<td></td>"
        stop_trs.append(
            f'<tr>'
            f'<td>{row.get("route_order","")}</td>'
            f'<td>{slabel}{desc_html}</td>'
            f'<td>{row.get("pickup_time","")}</td>'
            f'<td>{row.get("students_at_stop","")}</td>'
            f'{names_td}'
            f'</tr>'
        )

    # --- Instructions (page 3): per-leg maps + short guide ---
    instr_html = ""
    if legs:
        n = len(route_rows)
        def _stop_label(sid):
            name, _, _ = stop_names.get(sid, ("", "", ""))
            return name or sid
        leg_blocks = []
        for i, leg in enumerate(legs):
            # Origin / destination labels from route order
            if trip == "am":
                oname = _stop_label(route_rows[i]["stop_id"]) if i < n else "學校"
                dname = _stop_label(route_rows[i+1]["stop_id"]) if i+1 < n else "學校"
            else:
                oname = "學校" if i == 0 else _stop_label(route_rows[i-1]["stop_id"])
                dname = _stop_label(route_rows[i]["stop_id"])
            km = leg.get("distance_m", 0) / 1000
            mins = int(leg.get("duration_s", 0) / 60)
            roads = " → ".join(_tc(r) for r in leg.get("roads", [])[:3])
            roads_html = f'<div class="leg-roads">經 {roads}</div>' if roads else ""
            loc_note = "出發點位置" if oname == "學校" else (
                "上車站位置" if trip == "am" else "下車站位置")
            maps = ""
            try:
                from map_image import point_map_base64, leg_map_base64
                origin = tuple(leg["origin"]); dest = tuple(leg["dest"])
                loc_img = point_map_base64(origin, oname, size="380*240")
                route_img = leg_map_base64(origin, dest, leg.get("polyline", ""),
                                           origin_name=oname, dest_name=dname,
                                           size="380*240")
                if loc_img:
                    maps += (f'<div class="leg-map-box">'
                             f'<div class="map-note">{loc_note}</div>'
                             f'<img src="{loc_img}"/></div>')
                if route_img:
                    maps += (f'<div class="leg-map-box">'
                             f'<div class="map-note">行車路線</div>'
                             f'<img src="{route_img}"/></div>')
            except Exception:
                pass
            if maps:
                maps = f'<div class="leg-maps">{maps}</div>'
            leg_blocks.append(
                f'<div class="leg">'
                f'{maps}'
                f'<div class="leg-text">'
                f'<b>第{i+1}段　{oname} → {dname}</b>'
                f'{roads_html}'
                f'<div class="leg-meta">{km:.1f} km · 約 {mins} 分鐘</div>'
                f'</div>'
                f'</div>'
            )
        instr_html = "".join(leg_blocks)
    elif instructions:
        instr_html = _concise_instructions(instructions, route_rows, trip, school, stop_names) or ""
    if not instr_html:
        instr_html = "<li>請按地圖行駛</li>"

    # --- Maps (page 3) ---
    map_html = ""
    if school_pt and polyline:
        try:
            from map_image import static_map_base64, student_area_map_base64
            stops_for_map = [{"lat": row["lat"], "lng": row["lng"],
                              "name": stop_names.get(row["stop_id"], ("",""))[0],
                              "time": row.get("pickup_time","")} for row in route_rows]
            img = static_map_base64(stops_for_map, school_pt, polyline)
            if img:
                map_html = f'<div class="map-box"><img src="{img}"/></div>'
        except Exception:
            pass
    if not map_html:
        map_html = _route_diagram_svg(route_rows)

    html = (TEMPLATE
        .replace("__ROUTE_LABEL__", route_label)
        .replace("__SCHOOL_NAME_LINE__", school_name_line)
        .replace("__SCHOOL_ADDR__", school_addr)
        .replace("__YEAR__", year)
        .replace("__FASTEST_SUMMARY__", r0.get("fastest_duration", "—"))
        .replace("__FASTEST_DETAIL__", _fmt_route(r0.get("fastest_distance_m"), r0.get("fastest_tolls")))
        .replace("__SHORTEST_SUMMARY__", r0.get("shortest_duration", "—"))
        .replace("__SHORTEST_DETAIL__", _fmt_route(r0.get("shortest_distance_m"), r0.get("shortest_tolls")))
        .replace("__TOLLFREE_SUMMARY__", r0.get("tollfree_duration", "—"))
        .replace("__TOLLFREE_DETAIL__", _fmt_route(r0.get("tollfree_distance_m"), r0.get("tollfree_tolls")))
        .replace("__STOP_COL_HEADER__", time_label)
        .replace("__ROSTER_ROWS__", "\n".join(roster_trs))
        .replace("__TRIP_TITLE__", trip_label)
        .replace("__TIME_HEADER__", time_header)
        .replace("__STOP_ROWS__", "\n".join(stop_trs))
        .replace("__ROUTE_MAP__", map_html)
        .replace("__DRIVING_INSTRUCTIONS__", instr_html)
    )
    return html


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def _html_to_pdf(html, out_path):
    """Render HTML to PDF. Returns True on success."""
    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(str(out_path))
        return True
    except Exception:
        pass
    html_path = out_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    for edge in [r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                 r"C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
        if Path(edge).exists():
            try:
                result = subprocess.run(
                    [edge, "--headless", "--disable-gpu", "--no-first-run",
                     f"--user-data-dir={OUTPUT_DIR / '.edge-profile'}",
                     "--no-pdf-header-footer",
                     f"--print-to-pdf={out_path}", html_path.resolve().as_uri()],
                    capture_output=True, timeout=60)
                if out_path.exists() and out_path.stat().st_size > 0:
                    return True
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# Generate all PDFs for a trip
# ---------------------------------------------------------------------------

def generate_all_pdfs(manifest_csv=None, students_csv=None, trip="am"):
    import json
    suffix = trip.upper()
    manifest_path = Path(manifest_csv) if manifest_csv else DATA_DIR / f"route_manifest_{trip}.csv"
    students_path = str(students_csv) if students_csv else str(DATA_DIR / f"students_with_stops_{trip}.csv")

    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}")
        return
    rows = list(csv.DictReader(open(manifest_path, encoding="utf-8-sig")))
    if not rows:
        print("Manifest is empty"); return

    stop_names = {}
    stop_labels = {}
    stops_path = DATA_DIR / f"stops_{trip}.csv"
    if stops_path.exists():
        for s in csv.DictReader(open(stops_path, encoding="utf-8")):
            desc = (s.get("desc") or "").strip() or (s.get("address") or "").strip()
            street = (s.get("street") or "").strip()
            stop_names[s["stop_id"]] = (_clean_stop(s.get("name", "")), _tc(desc), _tc(street))
            stop_labels[s["stop_id"]] = _tc((s.get("label") or "").strip())

    polylines, steps_all, legs_all = {}, {}, {}
    pl = DATA_DIR / f"route_polylines_{trip}.json"
    st = DATA_DIR / f"route_steps_{trip}.json"
    lg = DATA_DIR / f"route_legs_{trip}.json"
    if pl.exists():  polylines = json.load(open(pl, encoding="utf-8"))
    if st.exists():  steps_all = json.load(open(st, encoding="utf-8"))
    if lg.exists():  legs_all  = json.load(open(lg, encoding="utf-8"))

    schools_loc = {}
    schools_info = {}
    sp = DATA_DIR / "schools.csv"
    if sp.exists():
        for s in csv.DictReader(open(sp, encoding="utf-8")):
            if s.get("lat") and s.get("lng"):
                schools_loc[s["school"]] = (float(s["lat"]), float(s["lng"]))
            schools_info[s["school"]] = (s.get("school_cn", ""), s.get("address", ""))

    from collections import OrderedDict
    routes = OrderedDict()
    for row in rows:
        routes.setdefault((row["school"], row["route_number"]), []).append(row)

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"Generating {len(routes)} route PDFs ({suffix})...")
    for (school, route_num), stop_rows in routes.items():
        route_label = f"{route_num}"
        key = f"{school}|{route_num}"
        poly = polylines.get(key, {}).get("fastest", "")
        instr = steps_all.get(key, {}).get("fastest", [])
        legs  = legs_all.get(key, [])
        scn, sadd = schools_info.get(school, ("", ""))
        html = build_route_pdf(
            stop_rows, route_label, school, students_path,
            stop_names=stop_names, school_pt=schools_loc.get(school),
            polyline=poly, instructions=instr, legs=legs, trip=trip,
            school_cn=scn, school_addr=sadd, stop_labels=stop_labels,
        )
        out = OUTPUT_DIR / f"{route_num.lower()}-{school}-{suffix}.pdf"
        if _html_to_pdf(html, out): print(f"  {out.name}")
        else:
            Path(str(out).replace(".pdf", ".html")).write_text(html, encoding="utf-8")
            print(f"  {out.stem}.html (HTML fallback)")

    print(f"Outputs: {OUTPUT_DIR}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python make_pdfs.py <manifest> [students]")
        sys.exit(1)
    students = sys.argv[2] if len(sys.argv) > 2 else None
    generate_all_pdfs(sys.argv[1], students)

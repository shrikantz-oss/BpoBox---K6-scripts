"""HTML report for end-to-end scenario runs (run_e2e.py result JSON → standalone HTML).

  python -I e2e_report.py qat/results/e2e_load_*.json qat/results/e2e_peak_*.json -o qat/results/e2e_combined.html

One result → single-run report. Several → adds an execution matrix comparing them.

Graphs follow the usual load-test report set (JMeter dashboard / k6 / Gatling / LoadRunner):
active VUs, throughput, response time over time (p50/p90/p95/p99), percentile distribution,
response-time histogram, errors over time and by status code, response time vs throughput,
latency breakdown, network throughput, plus the pipeline graphs (arrivals vs created vs processed,
backlog, processing rate) and an aggregate transaction table with Apdex.
Time-series graphs need the k6 CSV written by run_e2e.py; older runs show the pipeline graphs only.
Server-side metrics (CPU, DB, Lambda) aren't collected; the report says so.
"""
import argparse
import csv
import gzip
import html
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

STATUS_ORDER = {"pass": 0, "info": 0, "warn": 1, "fail": 2}
UPLOAD_NAME = "POST /calls/upload-zip"
APDEX_T_MS = {UPLOAD_NAME: 10000}  # everything else: 500 ms
TIMING_METRICS = ["http_req_blocked", "http_req_connecting", "http_req_tls_handshaking",
                  "http_req_sending", "http_req_waiting", "http_req_receiving"]
TIMING_LABELS = ["Blocked", "Connecting", "TLS handshake", "Sending", "Waiting (TTFB)", "Receiving"]


# ─── k6 summary → flat numbers ───────────────────────────────────────────────
def k6_extract(summary: dict) -> dict:
    m = summary.get("metrics") or {}

    def v(name, stat):
        return ((m.get(name) or {}).get("values") or {}).get(stat)

    return {
        "calls_uploaded": int(v("calls_uploaded", "count") or 0),
        "zips_uploaded": int(v("zips_uploaded", "count") or 0),
        "upload_success": v("upload_success", "rate"),
        "upload_avg_s": v("upload_seconds", "avg"),
        "upload_p95_s": v("upload_seconds", "p(95)"),
        "upload_max_s": v("upload_seconds", "max"),
        "dropped_iterations": int(v("dropped_iterations", "count") or 0),
        "browse_med_ms": v("http_req_duration{scenario:browse}", "med"),
        "browse_p95_ms": v("http_req_duration{scenario:browse}", "p(95)"),
        "browse_p99_ms": v("http_req_duration{scenario:browse}", "p(99)"),
        "browse_max_ms": v("http_req_duration{scenario:browse}", "max"),
        "browse_error_rate": v("http_req_failed{scenario:browse}", "rate"),
        "http_reqs": int(v("http_reqs", "count") or 0),
    }


# ─── k6 CSV → compact time series (stored in the result JSON) ───────────────
def _pctl(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def k6_timeseries(csv_path, t0_epoch: float) -> dict:
    """Aggregate k6's per-sample CSV into buckets + per-transaction stats."""
    opener = gzip.open if str(csv_path).endswith(".gz") else open
    durs = defaultdict(list)           # name -> [(t, ms)]
    failed = defaultdict(list)         # name -> [(t, 0/1)]
    timing = defaultdict(lambda: defaultdict(list))  # name -> metric -> [ms]
    status, vus, sent, recv = Counter(), [], [], []
    scenario_of = {}
    with opener(csv_path, "rt", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = row["metric_name"]
            try:
                t, val = int(row["timestamp"]) - t0_epoch, float(row["metric_value"])
            except (TypeError, ValueError):
                continue
            name = row.get("name") or row.get("url") or "?"
            if m == "http_req_duration":
                durs[name].append((t, val))
                scenario_of[name] = row.get("scenario") or ""
            elif m == "http_req_failed":
                failed[name].append((t, val))
            elif m == "http_reqs":
                status[row.get("status") or "0"] += 1
            elif m in TIMING_METRICS:
                timing[name][m].append(val)
            elif m == "vus":
                vus.append((t, val))
            elif m == "data_sent":
                sent.append((t, val))
            elif m == "data_received":
                recv.append((t, val))
    all_t = [t for v in durs.values() for t, _ in v] + [t for t, _ in vus]
    if not all_t:
        return {}
    t_min, t_max = min(all_t), max(all_t)
    span = max(1, t_max - t_min)
    b = max(5, int(math.ceil(span / 150 / 5) * 5))     # ≤ ~150 buckets
    n = int(span // b) + 1
    idx = lambda t: min(n - 1, max(0, int((t - t_min) // b)))  # noqa: E731
    bt = [round(t_min + i * b + b / 2) for i in range(n)]

    browse = [(t, v) for name, lst in durs.items() if scenario_of.get(name) == "browse" for t, v in lst]
    upload = durs.get(UPLOAD_NAME, [])

    def by_bucket(pairs):
        bk = [[] for _ in range(n)]
        for t, v in pairs:
            bk[idx(t)].append(v)
        return [sorted(x) for x in bk]

    bb, ub = by_bucket(browse), by_bucket(upload)
    req_count = [0] * n
    for lst in durs.values():
        for t, _ in lst:
            req_count[idx(t)] += 1
    err_count = [0] * n
    for lst in failed.values():
        for t, v in lst:
            if v >= 1:
                err_count[idx(t)] += 1
    vus_b = [0] * n
    for t, v in vus:
        vus_b[idx(t)] = max(vus_b[idx(t)], v)

    def kb_rate(pairs):
        out = [0.0] * n
        for t, v in pairs:
            out[idx(t)] += v
        return [round(x / 1024 / b, 2) for x in out]

    r = lambda x: None if x is None else round(x, 1)  # noqa: E731
    series = {
        "bucket_s": b, "t": bt,
        "vus": vus_b, "rps": [round(c / b, 2) for c in req_count], "errors": err_count,
        "browse_p50": [r(_pctl(x, 50)) for x in bb], "browse_p90": [r(_pctl(x, 90)) for x in bb],
        "browse_p95": [r(_pctl(x, 95)) for x in bb], "browse_p99": [r(_pctl(x, 99)) for x in bb],
        "upload_avg": [r(sum(x) / len(x) / 1000) if x else None for x in ub],
        "upload_p95": [r(_pctl(x, 95) / 1000) if x else None for x in ub],
        "kb_sent_s": kb_rate(sent), "kb_recv_s": kb_rate(recv),
        "status_codes": dict(sorted(status.items())),
    }
    # Histogram of browse response times (ms), ~20 bins up to p99.
    bvals = sorted(v for _, v in browse)
    if bvals:
        top = max(1.0, _pctl(bvals, 99))
        step = max(10, int(math.ceil(top / 20 / 10) * 10))
        bins = Counter(min(int(v // step), 20) for v in bvals)
        series["histogram"] = {"step_ms": step, "counts": [bins.get(i, 0) for i in range(21)]}
    # Aggregate report per transaction.
    tx = []
    for name, lst in sorted(durs.items(), key=lambda kv: -len(kv[1])):
        vals = sorted(v for _, v in lst)
        fails = sum(1 for _, v in failed.get(name, []) if v >= 1)
        t_apdex = APDEX_T_MS.get(name, 500)
        sat = sum(1 for v in vals if v <= t_apdex)
        tol = sum(1 for v in vals if t_apdex < v <= 4 * t_apdex)
        tx.append({
            "name": name, "scenario": scenario_of.get(name, ""), "count": len(vals),
            "avg": r(sum(vals) / len(vals)), "min": r(vals[0]), "med": r(_pctl(vals, 50)),
            "p90": r(_pctl(vals, 90)), "p95": r(_pctl(vals, 95)), "p99": r(_pctl(vals, 99)), "max": r(vals[-1]),
            "error_rate": fails / len(vals), "rps": round(len(vals) / span, 3),
            "apdex": round((sat + tol / 2) / len(vals), 3), "apdex_t_ms": t_apdex,
            "percentiles": [r(_pctl(vals, p)) for p in range(0, 101, 5)],
            "breakdown": {m: r(sum(timing[name][m]) / len(timing[name][m])) if timing[name][m] else 0
                          for m in TIMING_METRICS},
        })
    series["transactions"] = tx
    # Upload window (CSV timestamps are request-end times): first upload start → last upload end.
    if upload:
        series["upload_first_start_s"] = round(min(t - v / 1000 for t, v in upload), 1)
        series["upload_last_end_s"] = round(max(t for t, _ in upload), 1)
    return series


# ─── Derived pipeline numbers + gates ────────────────────────────────────────
def _peak_rate(tl, key, window_s=120):
    """Highest calls/min over any window of at least window_s seconds."""
    best = 0.0
    for i in range(len(tl)):
        for j in range(i - 1, -1, -1):
            dt = tl[i]["t"] - tl[j]["t"]
            if dt >= window_s:
                best = max(best, (tl[i][key] - tl[j][key]) / (dt / 60))
                break
    return best


def _first_t(tl, key, target):
    for s in tl:
        if target and s[key] >= target:
            return s["t"]
    return None


def derive(r: dict) -> dict:
    tl, k = r.get("timeline") or [], r.get("k6") or {}
    up = r.get("uploaded_calls") or 0
    planned_peak = max([st["callsPerMin"] for st in r["plan"]["stages"]] + [r["plan"].get("startCallsPerMin") or 0])
    final = r.get("final") or (tl[-1] if tl else {"ingested": 0, "scored": 0, "skipped": 0, "processed": 0, "t": 0})
    d = {
        "planned_peak": planned_peak,
        "peak_ingest": _peak_rate(tl, "ingested"),
        "peak_processed": _peak_rate(tl, "processed"),
        "t_all_ingested": _first_t(tl, "ingested", up),
        "t_all_processed": _first_t(tl, "processed", up),
        "peak_backlog": max([s["ingested"] - s["processed"] for s in tl] or [0]),
        "final": final,
    }
    gates = []

    def gate(name, value, target, status):
        gates.append({"name": name, "value": value, "target": target, "status": status})

    us = k.get("upload_success")
    gate("Upload success rate", _pct(us), "≥ 99%",
         "pass" if us is not None and us >= .99 else "warn" if us is not None and us >= .95 else "fail")
    delivered = up / r["planned_calls"] if r.get("planned_calls") else 0
    gate("Planned load delivered", f"{up:,} / {r.get('planned_calls', 0):,} ({delivered:.0%})", "≥ 95%",
         "pass" if delivered >= .95 else "warn" if delivered >= .8 else "fail")
    p95 = k.get("upload_p95_s")
    gate("Upload latency p95", _secs(p95), "≤ 10 s",
         "pass" if p95 is not None and p95 <= 10 else "warn" if p95 is not None and p95 <= 30 else "fail")
    ing = d["peak_ingest"] / planned_peak if planned_peak else 0
    gate("Call creation keeps up (peak)", f"{d['peak_ingest']:,.0f}/min vs {planned_peak:,}/min", "≥ 90% of arrival",
         "pass" if ing >= .9 else "warn" if ing >= .6 else "fail")
    proc = d["peak_processed"] / planned_peak if planned_peak else 0
    gate("Scoring keeps up (peak)", f"{d['peak_processed']:,.0f}/min vs {planned_peak:,}/min", "≥ 90% of arrival",
         "pass" if proc >= .9 else "warn" if proc >= .5 else "fail")
    done = d["t_all_processed"] is not None
    gate("All uploaded calls processed", _dur(d["t_all_processed"]) if done else
         f"{final['processed']:,} / {up:,} — {r.get('end_reason')}", "100%", "pass" if done else "fail")
    bp = k.get("browse_p95_ms")
    gate("Site response p95 (while processing)", _ms(bp), "≤ 1,000 ms",
         "info" if bp is None else "pass" if bp <= 1000 else "warn" if bp <= 3000 else "fail")
    be = k.get("browse_error_rate")
    gate("Site error rate", _pct(be), "≤ 1%",
         "info" if be is None else "pass" if be <= .01 else "warn" if be <= .05 else "fail")
    d["gates"] = gates
    d["verdict"] = max((g["status"] for g in gates), key=lambda s: STATUS_ORDER[s], default="info")
    return d


# ─── Formatting helpers ──────────────────────────────────────────────────────
def _pct(x):
    return "n/a" if x is None else f"{x * 100:.2f}%"


def _secs(x):
    return "n/a" if x is None else f"{x:.1f} s"


def _ms(x):
    return "n/a" if x is None else f"{x:,.0f} ms"


def _dur(s):
    if s is None:
        return "n/a"
    s = int(s)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def _clock(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def e(x):
    return html.escape(str(x))


def _num(v, unit=""):
    if v is None:
        return "–"
    return f"{v:,.0f}{unit}" if abs(v) >= 100 else f"{v:,.1f}{unit}" if abs(v) >= 10 else f"{v:,.2f}{unit}"


LABEL = {"pass": "Pass", "warn": "Warn", "fail": "Fail", "info": "Info"}
VERDICT = {"pass": "Pass", "warn": "Conditional pass", "fail": "Fail", "info": "Incomplete"}


def badge(status, text=None):
    return f'<span class="badge {status}">{e(text or LABEL[status])}</span>'


# ─── SVG chart toolkit (one y-axis per chart; hover tooltips; table view) ────
W, H, PL, PR, PT, PB = 640, 240, 56, 14, 12, 30
WIDE_W = 1200  # full-width charts get a wider canvas so text stays the same size


def _nice_max(v):
    if not v or v <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def _xticks(xmax, fmt):
    steps = (15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200) if fmt == "time" else (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000)
    step = next((s for s in steps if xmax / s <= 8), steps[-1])
    return [i * step for i in range(int(xmax // step) + 1)]


def _tick(v, top):
    """Axis label: integers when the scale allows, otherwise just enough decimals."""
    if top >= 10:
        return f"{v:,.0f}"
    return f"{v:,.1f}" if top >= 1 else f"{v:,.2f}"


def _frame(ymax, yfmt, xmax, xfmt, xlab, W=W):
    sx = lambda x: PL + (W - PL - PR) * (x / xmax if xmax else 0)  # noqa: E731
    sy = lambda y: PT + (H - PT - PB) * (1 - (y / ymax if ymax else 0))  # noqa: E731
    g = []
    for i in range(5):
        v = ymax * i / 4
        g.append(f'<line class="grid" x1="{PL}" x2="{W - PR}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
                 f'<text class="tick" x="{PL - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{e(_tick(v, ymax))}</text>')
    ticks = _xticks(xmax, xfmt)
    sub_minute = len(ticks) > 1 and ticks[1] < 60
    for x in ticks:
        lab = (_clock(x) if sub_minute else f"{x // 60}m") if xfmt == "time" else xlab(x)
        g.append(f'<text class="tick" x="{sx(x):.1f}" y="{H - 10}" text-anchor="middle">{e(lab)}</text>')
    g.append(f'<line class="axis" x1="{PL}" x2="{W - PR}" y1="{H - PB}" y2="{H - PB}"/>')
    return sx, sy, "".join(g)


def _card(title, sub, svg, legend, table, wide=False):
    leg = ""
    if legend and len(legend) >= 2:
        leg = '<div class="legend">' + "".join(f'<span><i class="sw {c}"></i>{e(lbl)}</span>' for lbl, c in legend) + "</div>"
    tbl = f'<details class="data"><summary>Show data</summary><div class="table-wrap">{table}</div></details>' if table else ""
    return (f'<figure class="card chart-card{" wide" if wide else ""}"><figcaption><h3>{e(title)}</h3><p>{e(sub)}</p></figcaption>'
            f'{leg}{svg}{tbl}</figure>')


def _table(head, rows):
    return ("<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in rows) + "</tbody></table>")


def line_chart(title, sub, xs, series, yfmt, unit, xfmt="time", xlab=str, markers=None, wide=False, ymax=None):
    """series: [(label, css_class, [y|None...])] sharing xs. One y-axis."""
    vals = [v for _, _, ys in series for v in ys if v is not None]
    if not xs or not vals:
        return _card(title, sub, '<p class="muted empty">No data for this run.</p>', None, None, wide)
    W = WIDE_W if wide else globals()["W"]
    xmax = max(xs) or 1
    ymax = ymax or _nice_max(max(vals) * 1.05)
    sx, sy, frame = _frame(ymax, yfmt, xmax, xfmt, xlab, W)
    paths = []
    for _, cls, ys in series:
        segs, cur = [], []
        for x, y in zip(xs, ys):
            if y is None:
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(f"{sx(x):.1f},{sy(min(y, ymax)):.1f}")
        if cur:
            segs.append(cur)
        for s in segs:
            if len(s) == 1:
                cx, cy = s[0].split(",")
                paths.append(f'<circle class="dot {cls}" cx="{cx}" cy="{cy}" r="3"/>')
            else:
                paths.append(f'<path class="ln {cls}" d="M{" L".join(s)}"/>')
    mk = ""
    for mx, mlabel in markers or []:
        if mx is not None and 0 <= mx <= xmax:
            near_end = sx(mx) > W - PR - 90  # flip the label inside the plot near the right edge
            mk += (f'<line class="marker" x1="{sx(mx):.1f}" x2="{sx(mx):.1f}" y1="{PT}" y2="{H - PB}"/>'
                   f'<text class="tick" x="{sx(mx) + (-5 if near_end else 5):.1f}" y="{PT + 11}" '
                   f'text-anchor="{"end" if near_end else "start"}">{e(mlabel)}</text>')
    # Hover columns: one per x, tooltip lists every series.
    hits, n = [], len(xs)
    for i, x in enumerate(xs):
        left = sx((xs[i - 1] + x) / 2) if i else PL
        right = sx((x + xs[i + 1]) / 2) if i < n - 1 else W - PR
        xl = _clock(x) if xfmt == "time" else xlab(x)
        tip = f"<b>{e(xl)}</b>" + "".join(
            f'<br><i class="sw {cls}"></i>{e(lbl)}: {e(_num(ys[i], unit))}' for lbl, cls, ys in series)
        hits.append(f'<rect class="hit" x="{left:.1f}" y="{PT}" width="{max(1, right - left):.1f}" height="{H - PT - PB}" '
                    f'data-x="{sx(x):.1f}" data-tip="{html.escape(tip, quote=True)}"/>')
    svg = (f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{e(title)}">{frame}{mk}{"".join(paths)}'
           f'<line class="cross" x1="0" x2="0" y1="{PT}" y2="{H - PB}" style="display:none"/>{"".join(hits)}</svg>')
    head = ["Time" if xfmt == "time" else "x"] + [f"{lbl} ({unit.strip()})" if unit.strip() else lbl for lbl, _, _ in series]
    rows = [[_clock(x) if xfmt == "time" else xlab(x)] + [_num(ys[i]) for _, _, ys in series] for i, x in enumerate(xs)]
    return _card(title, sub, svg, [(lbl, cls) for lbl, cls, _ in series], _table(head, rows), wide)


def bar_chart(title, sub, labels, values, cls, yfmt, unit, wide=False, show_values=False, bar_max=72):
    """Vertical bars, single series (status class allowed for errors)."""
    if not values or not any(values):
        return _card(title, sub, '<p class="muted empty">None recorded in this run.</p>', None,
                     _table(["Category", "Value"], list(zip(labels, values))) if values else None, wide)
    W = WIDE_W if wide else globals()["W"]
    ymax = _nice_max(max(values) * (1.18 if show_values else 1.05))
    n = len(values)
    bw = min(bar_max, (W - PL - PR) / n)     # cap so 1–2 categories don't become slabs
    x0 = PL + ((W - PL - PR) - bw * n) / 2   # centre the bar group
    sy = lambda y: PT + (H - PT - PB) * (1 - y / ymax)  # noqa: E731
    g = []
    for i in range(5):
        v = ymax * i / 4
        g.append(f'<line class="grid" x1="{PL}" x2="{W - PR}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
                 f'<text class="tick" x="{PL - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{e(_tick(v, ymax))}</text>')
    every = max(1, math.ceil(n / 10))
    bars = []
    for i, (lab, v) in enumerate(zip(labels, values)):
        x = x0 + i * bw + 1
        w = max(1, bw - 2)  # 2px surface gap between bars
        y = sy(v)
        h = (H - PB) - y
        r = min(4, w / 2, h)
        if h > 0:
            # rounded data-end, square at the baseline
            bars.append(f'<path class="bar {cls}" d="M{x:.1f},{H - PB} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                        f'H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{H - PB} Z"/>')
        if show_values and h > 0:  # direct value label above each bar (few bars only)
            bars.append(f'<text class="tick val" x="{x + w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle">{e(_num(v))}</text>')
        if i % every == 0:
            bars.append(f'<text class="tick" x="{x + w / 2:.1f}" y="{H - 10}" text-anchor="middle">{e(lab)}</text>')
        tip = f"<b>{e(lab)}</b><br>{e(_num(v, unit))}"
        bars.append(f'<rect class="hit" x="{x - 1:.1f}" y="{PT}" width="{bw:.1f}" height="{H - PT - PB}" data-tip="{html.escape(tip, quote=True)}"/>')
    g.append(f'<line class="axis" x1="{PL}" x2="{W - PR}" y1="{H - PB}" y2="{H - PB}"/>')
    svg = f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{e(title)}">{"".join(g)}{"".join(bars)}</svg>'
    return _card(title, sub, svg, None, _table(["Category", f"Value ({unit.strip()})"], [[a, _num(b)] for a, b in zip(labels, values)]), wide)


def stacked_hbar(title, sub, rows, seg_labels, seg_classes, unit):
    """rows: [(label, [v per segment])] — horizontal 100%-length-by-value stacks."""
    if not rows:
        return _card(title, sub, '<p class="muted empty">No data for this run.</p>', None, None)
    W = WIDE_W
    LW = 230
    rh, gap = 22, 14
    h = PT + len(rows) * (rh + gap) + 24
    tot_max = _nice_max(max(sum(v) for _, v in rows) * 1.05)
    sx = lambda v: LW + (W - LW - PR) * v / tot_max  # noqa: E731
    g = []
    for i in range(5):
        v = tot_max * i / 4
        g.append(f'<line class="grid" x1="{sx(v):.1f}" x2="{sx(v):.1f}" y1="{PT}" y2="{h - 22}"/>'
                 f'<text class="tick" x="{sx(v):.1f}" y="{h - 8}" text-anchor="middle">{e(_tick(v, tot_max))}</text>')
    for ri, (lab, vals) in enumerate(rows):
        y = PT + ri * (rh + gap)
        g.append(f'<text class="tick lab" x="{LW - 8}" y="{y + rh / 2 + 4:.1f}" text-anchor="end">{e(lab)}</text>')
        x = LW
        for si, v in enumerate(vals):
            if v <= 0:
                continue
            w = sx(v) - LW
            tip = f"<b>{e(lab)}</b><br>{e(seg_labels[si])}: {e(_num(v, unit))}"
            g.append(f'<rect class="bar {seg_classes[si]}" x="{x:.1f}" y="{y}" width="{max(0.5, w - 2):.1f}" height="{rh}" rx="2" '
                     f'data-tip="{html.escape(tip, quote=True)}"/>')
            x += w
        g.append(f'<text class="tick" x="{x + 6:.1f}" y="{y + rh / 2 + 4:.1f}">{e(_num(sum(vals), unit))}</text>')
    svg = f'<svg class="chart" viewBox="0 0 {W} {h}" role="img" aria-label="{e(title)}">{"".join(g)}</svg>'
    table = _table(["Transaction"] + seg_labels + ["Total"], [[lab] + [_num(v) for v in vals] + [_num(sum(vals))] for lab, vals in rows])
    return _card(title, sub, svg, list(zip(seg_labels, seg_classes)), table, wide=True)


def scatter(title, sub, pts, xlab, ylab, unit_y):
    """pts: [(x, y)] one series."""
    pts = [(x, y) for x, y in pts if x is not None and y is not None]
    if not pts:
        return _card(title, sub, '<p class="muted empty">No data for this run.</p>', None, None)
    xmax = _nice_max(max(x for x, _ in pts) * 1.05)
    ymax = _nice_max(max(y for _, y in pts) * 1.05)
    sx = lambda x: PL + (W - PL - PR) * x / xmax  # noqa: E731
    sy = lambda y: PT + (H - PT - PB) * (1 - y / ymax)  # noqa: E731
    g = []
    for i in range(5):
        v, xv = ymax * i / 4, xmax * i / 4
        g.append(f'<line class="grid" x1="{PL}" x2="{W - PR}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
                 f'<text class="tick" x="{PL - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{e(_tick(v, ymax))}</text>'
                 f'<text class="tick" x="{sx(xv):.1f}" y="{H - 10}" text-anchor="middle">{e(_tick(xv, xmax))}</text>')
    g.append(f'<line class="axis" x1="{PL}" x2="{W - PR}" y1="{H - PB}" y2="{H - PB}"/>')
    for x, y in pts:
        tip = f"{e(xlab)}: {e(_num(x))}<br>{e(ylab)}: {e(_num(y, unit_y))}"
        g.append(f'<circle class="pt s1" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4.5" data-tip="{html.escape(tip, quote=True)}"/>')
    svg = (f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{e(title)}">{"".join(g)}'
           f'<text class="tick" x="{W - PR}" y="{H - PB - 6}" text-anchor="end">{e(xlab)} →</text></svg>')
    return _card(title, sub, svg, None, _table([xlab, ylab], [[_num(x), _num(y)] for x, y in pts]))


# ─── Report sections ─────────────────────────────────────────────────────────
ADVICE = {
    "Scoring keeps up (peak)": "Pipeline throughput is below the arrival rate, so a backlog builds. Raise transcription_svc "
                               "reserved concurrency (50 ≈ 40 calls/min at ~75 s per call) and the scoring/violations/KPI "
                               "Lambdas, and confirm AssemblyAI and LLM rate limits.",
    "Call creation keeps up (peak)": "Zip extraction runs one file at a time in a FastAPI BackgroundTask inside the API. "
                                     "Move it to parallel workers (Celery or S3-event Lambda).",
    "All uploaded calls processed": "Some calls never finished. Check the scoring/violations/KPI DLQs and analysis_jobs "
                                    "rows with status='partial_failure' for this run's filenames.",
    "Upload latency p95": "Uploads are slow: check API CPU, ALB/nginx limits and network from the load generator.",
    "Upload success rate": "Uploads failed: check API error logs, auth (single-session policy), and ALB 5xx.",
    "Planned load delivered": "The load generator didn't deliver the plan (dropped iterations or failed uploads); "
                              "run k6 closer to the API (EC2 in-region) or raise maxVUs.",
    "Site response p95 (while processing)": "Users feel the batch: background processing competes with page requests "
                                            "on the API servers.",
    "Site error rate": "Page requests failed during processing: check API 5xx and connection errors.",
}


def findings(d: dict) -> str:
    bad = [g for g in d["gates"] if g["status"] in ("fail", "warn")]
    if not bad:
        return ('<div class="alert a-pass"><div class="stripe"></div><div class="body"><div class="title">'
                f'{badge("pass", "Validated")}<h3>All gates passed</h3></div></div></div>')
    out = []
    for g in sorted(bad, key=lambda g: -STATUS_ORDER[g["status"]]):
        cls = "a-fail" if g["status"] == "fail" else "a-warn"
        out.append(f'<div class="alert {cls}"><div class="stripe"></div><div class="body"><div class="title">'
                   f'{badge(g["status"], "Bottleneck" if g["status"] == "fail" else "Warning")}<h3>{e(g["name"])}: {e(g["value"])}</h3></div>'
                   f'<ul><li><strong>Target:</strong> {e(g["target"])}</li><li><strong>Recommendation:</strong> {e(ADVICE.get(g["name"], ""))}</li></ul>'
                   '</div></div>')
    return "".join(out)


def apdex_label(a):
    if a is None:
        return "info", "n/a"
    for lim, lab, st in ((.94, "Excellent", "pass"), (.85, "Good", "pass"), (.70, "Fair", "warn"), (.50, "Poor", "fail")):
        if a >= lim:
            return st, lab
    return "fail", "Unacceptable"


def pipeline_charts(r, d) -> str:
    tl, plan = r.get("timeline") or [], r.get("planned_curve") or []
    ue = r.get("upload_end_s")
    marks = [(ue, "uploads end")] if ue else None
    # Cumulative: planned is on its own (denser) grid, so resample it onto the poll times.
    def planned_at(t):
        prev = (0, 0.0)
        for p in plan:
            if p[0] >= t:
                if p[0] == prev[0]:
                    return p[1]
                return prev[1] + (p[1] - prev[1]) * (t - prev[0]) / (p[0] - prev[0])
            prev = p
        return plan[-1][1] if plan else None
    xs = [s["t"] for s in tl]
    cum = line_chart("Calls over time (cumulative)", "Planned arrivals vs calls created vs processed (scored + skipped)",
                     xs, [("Planned arrivals", "s4", [planned_at(t) for t in xs]),
                          ("Created", "s1", [s["ingested"] for s in tl]),
                          ("Processed", "s3", [s["processed"] for s in tl])],
                     _num, " calls", markers=marks)
    backlog = line_chart("Backlog", "Calls created but not yet processed — should return to zero",
                         xs, [("Backlog", "s2", [s["ingested"] - s["processed"] for s in tl])], _num, " calls", markers=marks)
    # Rolling 60 s window: per-poll deltas jump whenever a whole zip lands between two polls.
    rx, created, processed = [], [], []
    for i, b in enumerate(tl):
        a = next((tl[j] for j in range(i - 1, -1, -1) if b["t"] - tl[j]["t"] >= 60), None)
        if a:
            dt = (b["t"] - a["t"]) / 60
            rx.append(b["t"])
            created.append(round((b["ingested"] - a["ingested"]) / dt, 1))
            processed.append(round((b["processed"] - a["processed"]) / dt, 1))
    rate = line_chart("Pipeline throughput", f"Calls per minute, rolling 60 s window · planned peak {d['planned_peak']:,}/min",
                      rx, [("Created/min", "s1", created), ("Processed/min", "s3", processed)], _num, "/min", markers=marks, wide=True)
    return f'<div class="grid2">{cum}{backlog}</div><div class="grid1">{rate}</div>'


def k6_charts(r) -> str:
    s = r.get("k6_series") or {}
    if not s:
        return ('<p class="note">Request-level graphs (response time over time, throughput, VUs, errors, percentiles, '
                'latency breakdown) need the k6 CSV, which this run predates. Re-run the scenario with the current scripts.</p>')
    t, ue = s["t"], r.get("upload_end_s")
    marks = [(ue, "uploads end")] if ue else None
    b = s["bucket_s"]
    out = ['<h3 class="group">Load &amp; throughput</h3><div class="grid2">',
           line_chart("Active virtual users", f"Max VUs per {b}s", t, [("VUs", "s1", s["vus"])], _num, "", markers=marks),
           line_chart("Throughput", f"HTTP requests per second · {b}s buckets", t, [("Requests/s", "s1", s["rps"])],
                      _num, " req/s", markers=marks),
           "</div>",
           '<h3 class="group">Response times</h3><div class="grid1">',
           line_chart("Site response time over time", "Call Review pages + health check, percentiles per bucket",
                      t, [("p50", "s1", s["browse_p50"]), ("p90", "s2", s["browse_p90"]),
                          ("p95", "s3", s["browse_p95"]), ("p99", "s4", s["browse_p99"])], _num, " ms", markers=marks, wide=True),
           "</div><div class=\"grid2\">",
           line_chart("Upload response time over time", "POST /calls/upload-zip (one zip per request)",
                      t, [("Average", "s1", s["upload_avg"]), ("p95", "s2", s["upload_p95"])], _num, " s", markers=marks)]
    tx = s.get("transactions") or []
    browse_tx = [x for x in tx if x["name"] != UPLOAD_NAME][:4]
    pct_x = list(range(0, 101, 5))
    out.append(line_chart("Response time percentiles", "Distribution per page (x = percentile)", pct_x,
                          [(x["name"], f"s{i + 1}", x["percentiles"]) for i, x in enumerate(browse_tx)],
                          _num, " ms", xfmt="pct", xlab=lambda v: f"p{v}"))
    out.append("</div><div class=\"grid2\">")
    hist = s.get("histogram")
    if hist:
        step = hist["step_ms"]
        labels = [f"{i * step}–{(i + 1) * step}" if i < 20 else f"≥{20 * step}" for i in range(21)]
        out.append(bar_chart("Response time distribution", f"Site requests per {step} ms bucket", labels, hist["counts"],
                             "s1", _num, " requests"))
    pts = [(rps, p95) for rps, p95 in zip(s["rps"], s["browse_p95"])]
    out.append(scatter("Response time vs throughput", "Each dot = one time bucket · p95 site response vs total req/s",
                       pts, "Requests/s", "p95 response", " ms"))
    out.append("</div>")
    out.append('<h3 class="group">Errors</h3><div class="grid2">')
    out.append(line_chart("Errors over time", f"Failed HTTP requests per {b}s", t, [("Errors", "err", s["errors"])],
                          _num, " errors", markers=marks))
    codes = s.get("status_codes") or {}
    out.append(bar_chart("Responses by status code", "All HTTP requests in the run (0 = no response)",
                         list(codes.keys()), list(codes.values()), "s1", _num, " requests"))
    out.append("</div>")
    rows = [(x["name"], [x["breakdown"][m] or 0 for m in TIMING_METRICS]) for x in tx]
    out.append('<h3 class="group">Latency breakdown &amp; network</h3><div class="grid1">')
    out.append(stacked_hbar("Latency breakdown by transaction", "Average ms per phase (Waiting = server time to first byte)",
                            rows, TIMING_LABELS, ["s1", "s2", "s3", "s4", "s5", "s6"], " ms"))
    out.append("</div><div class=\"grid1\">")
    out.append(line_chart("Network throughput", "KB/s sent (uploads) and received", t,
                          [("Sent", "s1", s["kb_sent_s"]), ("Received", "s2", s["kb_recv_s"])], _num, " KB/s", markers=marks, wide=True))
    out.append("</div>")
    # Aggregate report
    trs = []
    for x in tx:
        st, lab = apdex_label(x["apdex"])
        unit_s = x["name"] == UPLOAD_NAME
        f = (lambda v: f"{v / 1000:,.2f} s") if unit_s else (lambda v: f"{v:,.0f} ms")
        trs.append(f"<tr><td><span class=\"name\">{e(x['name'])}</span><span class=\"desc\">{e(x['scenario'])}</span></td>"
                   f"<td>{x['count']:,}</td><td>{f(x['avg'])}</td><td>{f(x['min'])}</td><td>{f(x['med'])}</td>"
                   f"<td>{f(x['p90'])}</td><td>{f(x['p95'])}</td><td>{f(x['p99'])}</td><td>{f(x['max'])}</td>"
                   f"<td>{x['error_rate'] * 100:.2f}%</td><td>{x['rps']:.2f}</td>"
                   f"<td>{x['apdex']:.2f} {badge(st, lab)}<span class=\"desc\">T = {x['apdex_t_ms'] / 1000:g} s</span></td></tr>")
    out.append('<h3 class="group">Aggregate report</h3><div class="card table-wrap"><table class="agg"><thead><tr><th>Transaction</th>'
               '<th>Samples</th><th>Avg</th><th>Min</th><th>Median</th><th>p90</th><th>p95</th><th>p99</th><th>Max</th>'
               f'<th>Error %</th><th>Req/s</th><th>Apdex</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div>')
    return "".join(out)


def run_section(r: dict, d: dict) -> str:
    k, f = r.get("k6") or {}, d["final"]
    cards1 = [
        ("Calls uploaded", f"{r.get('uploaded_calls', 0):,}", f"{k.get('zips_uploaded', 0):,} zips × {r['calls_per_zip']} · planned {r.get('planned_calls', 0):,}"),
        ("Peak arrival", f"{d['peak_ingest']:,.0f}<small>calls/min</small>", f"Planned peak {d['planned_peak']:,}/min (calls created)"),
        ("Upload success rate", _pct(k.get("upload_success")), f"{k.get('dropped_iterations', 0)} dropped iterations"),
        ("Avg upload latency", _secs(k.get("upload_avg_s")), f"p95 {_secs(k.get('upload_p95_s'))} · max {_secs(k.get('upload_max_s'))}"),
    ]
    cards2 = [
        ("End-to-end: all processed", _dur(d["t_all_processed"]), f"All created at {_dur(d['t_all_ingested'])} · from first upload"),
        ("Peak scoring throughput", f"{d['peak_processed']:,.0f}<small>calls/min</small>", f"{f['scored']:,} scored · {f['skipped']:,} skipped"),
        ("Peak backlog", f"{d['peak_backlog']:,}<small>calls</small>", "Created but not yet processed"),
        ("Site p95 while processing", _ms(k.get("browse_p95_ms")), f"p50 {_ms(k.get('browse_med_ms'))} · errors {_pct(k.get('browse_error_rate'))}"),
    ]

    def cards(items):
        return '<div class="scorecards">' + "".join(
            f'<div class="card kpi"><div class="label">{e(a)}</div><div class="value">{b}</div><div class="foot">{e(c)}</div></div>'
            for a, b, c in items) + "</div>"

    gates = "".join(f"<tr><td>{badge(g['status'])}</td><td>{e(g['name'])}</td><td>{e(g['value'])}</td><td>{e(g['target'])}</td></tr>"
                    for g in d["gates"])
    stages = ", ".join(f"{s['minutes']:g} min → {s['callsPerMin']}/min" for s in r["plan"]["stages"])
    return f"""
<div class="run">
  <div class="section-head"><h2>Scenario: {e(r['scenario'])} {badge(d['verdict'], VERDICT[d['verdict']])}</h2>
    <p>{e(r['description'])}</p></div>
  {cards(cards1)}{cards(cards2)}
  <section><div class="section-head"><h2>Gates</h2><p>Ended: {e(r.get('end_reason'))}</p></div>
    <div class="card table-wrap"><table class="gates"><thead><tr><th>Result</th><th>Check</th><th>Measured</th><th>Target</th></tr></thead><tbody>{gates}</tbody></table></div></section>
  <section><div class="section-head"><h2>Pipeline (end to end)</h2><p>Polled from the calls API · time from the start of the run</p></div>
    {pipeline_charts(r, d)}</section>
  <section><div class="section-head"><h2>Client-side performance</h2><p>Every HTTP request k6 made · hover for values, “Show data” for tables</p></div>
    {k6_charts(r)}</section>
  <section><div class="section-head"><h2>Findings &amp; recommendations</h2></div><div class="alerts">{findings(d)}</div>
    <p class="note">Load profile: start {r['plan'].get('startCallsPerMin', 0)}/min, then {e(stages)}. Run ID <span class="mono">{e(r['run_id'])}</span>
    (prefix of every uploaded filename) · {k.get('http_reqs', 0):,} HTTP requests · {r.get('poll_errors', 0)} progress-poll errors · k6 exit {r.get('k6_exit_code')}.
    Server-side metrics (API CPU/memory, DB connections, Lambda concurrency, SQS age) are not collected by these scripts: attach CloudWatch for the run window.</p></section>
</div>"""


CSS = """
:root{--bg:#f6f7f9;--surface:#fff;--surface-2:#f9fafb;--border:#e3e6eb;--border-strong:#cfd4dc;--text:#111827;--text-2:#4b5563;--text-3:#6b7280;--brand:#1f3a8a;
--pass:#0f7a3d;--pass-bg:#e8f6ee;--pass-bd:#b7e2c8;--warn:#9a5b00;--warn-bg:#fff4e0;--warn-bd:#f5d49a;--fail:#b42318;--fail-bg:#fdecea;--fail-bd:#f5bdb7;
--info:#1e4fb8;--info-bg:#eaf1ff;--info-bd:#bcd0fb;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--err:#d03b2f;
--radius:10px;--shadow:0 1px 2px rgba(16,24,40,.05);
--font:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:"JetBrains Mono",ui-monospace,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--surface:#161a21;--surface-2:#1b2029;--border:#2a313c;--border-strong:#3a4352;--text:#e8ebf0;--text-2:#b4bcc8;--text-3:#8b95a5;--brand:#8fb0ff;
--pass:#4ade80;--pass-bg:#0f2a1b;--pass-bd:#1f5a37;--warn:#fbbf24;--warn-bg:#2b2210;--warn-bd:#5c4614;--fail:#f87171;--fail-bg:#2c1515;--fail-bd:#5e2424;--info:#93b4ff;--info-bg:#141f36;--info-bd:#2a4275;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--err:#e66767;--shadow:none}}
:root[data-theme="dark"]{--bg:#0f1115;--surface:#161a21;--surface-2:#1b2029;--border:#2a313c;--border-strong:#3a4352;--text:#e8ebf0;--text-2:#b4bcc8;--text-3:#8b95a5;--brand:#8fb0ff;
--pass:#4ade80;--pass-bg:#0f2a1b;--pass-bd:#1f5a37;--warn:#fbbf24;--warn-bg:#2b2210;--warn-bd:#5c4614;--fail:#f87171;--fail-bg:#2c1515;--fail-bd:#5e2424;--info:#93b4ff;--info-bg:#141f36;--info-bd:#2a4275;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--err:#e66767;--shadow:none}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 var(--font);font-feature-settings:"tnum" 1}
.wrap{max-width:1240px;margin:0 auto;padding:32px 24px 56px}h1,h2,h3{margin:0;letter-spacing:-.01em}h2{font-size:17px;font-weight:600}h3{font-size:14px;font-weight:600}p{margin:0}
.muted{color:var(--text-3)}.mono{font-family:var(--mono);font-size:12.5px}section{margin-top:28px}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap}.section-head p{color:var(--text-3);font-size:13px}
h3.group{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);margin:22px 0 10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
.doc-head{display:grid;grid-template-columns:1fr auto;gap:20px;align-items:start;padding-bottom:22px;border-bottom:1px solid var(--border)}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--brand)}.doc-head h1{font-size:26px;font-weight:700;margin-top:6px}
.doc-head .sub{color:var(--text-2);margin-top:6px;max-width:72ch}.verdict{text-align:right}.verdict .big{display:inline-flex;font-weight:700;font-size:15px;padding:8px 14px;border-radius:8px;border:1px solid}
.meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:0}.meta div{padding:12px 16px;border-right:1px solid var(--border)}.meta div:last-child{border-right:0}
.meta dt{font-size:11.5px;color:var(--text-3);text-transform:uppercase;letter-spacing:.06em}.meta dd{margin:3px 0 0;font-weight:500;overflow-wrap:anywhere}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.pass{color:var(--pass);background:var(--pass-bg);border-color:var(--pass-bd)}.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-bd)}
.fail{color:var(--fail);background:var(--fail-bg);border-color:var(--fail-bd)}.info{color:var(--info);background:var(--info-bg);border-color:var(--info-bd)}
.scorecards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.scorecards+.scorecards{margin-top:14px}
.kpi{padding:16px 18px}.kpi .label{font-size:12.5px;color:var(--text-3);font-weight:500}.kpi .value{font-size:26px;font-weight:700;margin-top:6px;letter-spacing:-.02em}
.kpi .value small{font-size:13px;font-weight:600;color:var(--text-3);margin-left:3px}.kpi .foot{margin-top:8px;font-size:12.5px;color:var(--text-2)}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{background:var(--surface-2);font-size:11.5px;font-weight:600;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border-strong)}
tbody tr:last-child td{border-bottom:0}td .name{font-weight:600}td .desc{display:block;font-size:12px;color:var(--text-3)}
.gates td:nth-child(2),.gates td:nth-child(3),.gates th:nth-child(2),.gates th:nth-child(3){text-align:left;white-space:normal}.gates td:first-child{width:80px}
.agg td:last-child,.agg th:last-child{text-align:left}.agg td:last-child .badge{margin-left:6px}
.grid1,.grid2{display:grid;gap:14px;margin-bottom:14px}.grid2{grid-template-columns:repeat(2,minmax(0,1fr))}
.chart-card{margin:0;padding:14px 16px 12px;min-width:0}.chart-card figcaption h3{font-size:14px}.chart-card figcaption p{font-size:12.5px;color:var(--text-3);margin-top:2px}
.chart{width:100%;height:auto;display:block;margin-top:8px;overflow:visible}
.chart .grid{stroke:var(--border);stroke-width:1}.chart .axis{stroke:var(--border-strong);stroke-width:1}
.chart .tick{fill:var(--text-3);font-size:11px;font-family:var(--font)}.chart .tick.lab{fill:var(--text-2);font-size:12px}.chart .tick.val{fill:var(--text);font-size:12px;font-weight:600}
.chart .marker{stroke:var(--text-3);stroke-dasharray:4 4}.chart .cross{stroke:var(--text-3);stroke-width:1}
.chart .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.chart .hit{fill:transparent;cursor:crosshair}.chart .pt{stroke:var(--surface);stroke-width:2}
.ln.s1{stroke:var(--s1)}.ln.s2{stroke:var(--s2)}.ln.s3{stroke:var(--s3)}.ln.s4{stroke:var(--s4)}.ln.err{stroke:var(--err)}
.dot.s1,.bar.s1,.pt.s1,.sw.s1{fill:var(--s1);background:var(--s1)}.dot.s2,.bar.s2,.sw.s2{fill:var(--s2);background:var(--s2)}
.dot.s3,.bar.s3,.sw.s3{fill:var(--s3);background:var(--s3)}.dot.s4,.bar.s4,.sw.s4{fill:var(--s4);background:var(--s4)}
.bar.s5,.sw.s5{fill:var(--s5);background:var(--s5)}.bar.s6,.sw.s6{fill:var(--s6);background:var(--s6)}.dot.err,.sw.err{fill:var(--err);background:var(--err)}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12.5px;color:var(--text-2)}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
details.data{margin-top:8px;font-size:12.5px}details.data summary{cursor:pointer;color:var(--text-3)}details.data table{margin-top:6px;font-size:12px}
details.data .table-wrap{max-height:260px;overflow:auto;border:1px solid var(--border);border-radius:8px}
.empty{padding:28px 0;text-align:center}
#tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface);color:var(--text);border:1px solid var(--border-strong);border-radius:8px;
padding:8px 10px;font-size:12.5px;line-height:1.5;box-shadow:0 4px 16px rgba(0,0,0,.12);max-width:280px}
#tip .sw{width:8px;height:8px;margin-right:5px}
.alerts{display:grid;gap:12px}.alert{display:grid;grid-template-columns:4px 1fr;border-radius:var(--radius);border:1px solid var(--border);background:var(--surface);overflow:hidden}
.alert .stripe{background:var(--text-3)}.alert.a-fail .stripe{background:var(--fail)}.alert.a-warn .stripe{background:var(--warn)}.alert.a-pass .stripe{background:var(--pass)}
.alert .body{padding:14px 18px}.alert .title{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.alert ul{margin:8px 0 0;padding-left:18px;color:var(--text-2)}
.note{margin-top:12px;font-size:12.5px;padding:8px 12px;border-radius:8px;background:var(--info-bg);border:1px solid var(--info-bd);color:var(--info)}
.run{margin-top:40px;padding-top:8px;border-top:1px solid var(--border)}footer{margin-top:32px;font-size:12px;color:var(--text-3)}
@media (max-width:900px){.scorecards{grid-template-columns:repeat(2,minmax(0,1fr))}.meta{grid-template-columns:repeat(2,minmax(0,1fr))}.meta div:nth-child(2n){border-right:0}.grid2{grid-template-columns:1fr}
.chart-card.wide{overflow-x:auto}.chart-card.wide .chart{min-width:760px}}
@media (max-width:560px){.wrap{padding:20px 16px 40px}.doc-head{grid-template-columns:1fr}.verdict{text-align:left}.scorecards,.meta{grid-template-columns:1fr}
.meta div{border-right:0;border-bottom:1px solid var(--border)}.kpi .value{font-size:22px}}
@media print{body{background:#fff;font-size:12px}.wrap{max-width:none;padding:0}.card,.alert,.chart-card{box-shadow:none;break-inside:avoid}details.data{display:none}}
@media (forced-colors:active){.chart .ln{stroke:CanvasText}.bar,.dot,.pt{fill:CanvasText}}
"""

JS = """
(function(){var tip=document.getElementById('tip');
document.querySelectorAll('svg.chart').forEach(function(svg){var cross=svg.querySelector('.cross');
function hide(){tip.hidden=true;if(cross)cross.style.display='none';}
svg.addEventListener('mousemove',function(ev){var t=ev.target.closest('[data-tip]');if(!t){hide();return;}
tip.innerHTML=t.getAttribute('data-tip');tip.hidden=false;
var x=ev.clientX+14,y=ev.clientY+14,r=tip.getBoundingClientRect();
if(x+r.width>innerWidth-8)x=ev.clientX-r.width-14;if(y+r.height>innerHeight-8)y=ev.clientY-r.height-14;
tip.style.left=x+'px';tip.style.top=y+'px';
if(cross&&t.dataset.x){cross.setAttribute('x1',t.dataset.x);cross.setAttribute('x2',t.dataset.x);cross.style.display='';}});
svg.addEventListener('mouseleave',hide);});})();
"""


def render(results: list) -> str:
    rows = [(r, derive(r)) for r in results]
    overall = max((d["verdict"] for _, d in rows), key=lambda s: STATUS_ORDER[s], default="info")
    first, last = results[0], results[-1]
    host = urlparse(first.get("base_url", "")).netloc or first.get("base_url", "")
    matrix = ""
    if len(rows) > 1:
        trs = "".join(
            f"<tr><td><span class=\"name\">{e(r['scenario'])}</span><span class=\"desc\">{e(r['description'])}</span></td>"
            f"<td>{r.get('uploaded_calls', 0):,}</td><td>{_pct((r.get('k6') or {}).get('upload_success'))}</td>"
            f"<td>{_secs((r.get('k6') or {}).get('upload_p95_s'))}</td><td>{d['planned_peak']:,}</td><td>{d['peak_ingest']:,.0f}</td>"
            f"<td>{d['peak_processed']:,.0f}</td><td>{d['peak_backlog']:,}</td><td>{_dur(d['t_all_processed'])}</td>"
            f"<td>{_ms((r.get('k6') or {}).get('browse_p95_ms'))}</td><td>{badge(d['verdict'], VERDICT[d['verdict']])}</td></tr>"
            for r, d in rows)
        matrix = f"""<section><div class="section-head"><h2>Execution matrix</h2><p>Rates in calls/min</p></div>
<div class="card table-wrap"><table><thead><tr><th>Scenario</th><th>Uploaded</th><th>Upload OK</th><th>Upload p95</th><th>Planned peak</th>
<th>Peak created</th><th>Peak processed</th><th>Peak backlog</th><th>All processed in</th><th>Site p95</th><th>Result</th></tr></thead><tbody>{trs}</tbody></table></div></section>"""
    body = "".join(run_section(r, d) for r, d in rows)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>E2E Performance Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><main class="wrap">
<header class="doc-head"><div><div class="eyebrow">End-to-end performance report</div><h1>BPO-BOX Call Upload → Scoring Pipeline</h1>
<p class="sub">Calls uploaded as zips through <span class="mono">POST /api/calls/upload-zip</span> and tracked until scored or skipped,
with simulated users browsing the Call Review pages during processing.</p></div>
<div class="verdict"><div class="muted" style="font-size:12px;margin-bottom:6px">Overall verdict</div><span class="big {overall}">{VERDICT[overall]}</span></div></header>
<div class="card" style="margin-top:18px"><dl class="meta">
<div><dt>Environment</dt><dd>{e(host)}</dd></div><div><dt>Group</dt><dd class="mono">{e(first.get('group_id'))}</dd></div>
<div><dt>Window (UTC)</dt><dd>{e(first.get('started_utc', '')[:16].replace('T', ' '))} → {e(last.get('finished_utc', '')[:16].replace('T', ' '))}</dd></div>
<div><dt>Scenarios</dt><dd>{e(', '.join(r['scenario'] for r in results))}</dd></div></dl></div>
{matrix}{body}
<footer>Generated by e2e_report.py from run_e2e.py results · k6 + calls API polling</footer>
</main><div id="tip" hidden></div><script>{JS}</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("qat/results/e2e_report.html"))
    args = ap.parse_args()
    results = []
    for p in args.results:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            print(f"Cannot read {p}: {err}", file=sys.stderr)
            return 1
        # Older result JSONs: build the series from the CSV if it's next to them.
        csv_file = p.parent / (r.get("k6_csv") or "")
        if not r.get("k6_series") and r.get("k6_csv") and r.get("t0_epoch") and csv_file.is_file():
            r["k6_series"] = k6_timeseries(csv_file, r["t0_epoch"])
        results.append(r)
    results.sort(key=lambda r: r.get("started_utc", ""))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(results), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

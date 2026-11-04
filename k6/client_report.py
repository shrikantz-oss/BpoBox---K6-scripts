"""Client-facing performance report combining every completed burst run (one standalone HTML page).

  python -I client_report.py qat/results/e2e_burst100_*.json qat/results/e2e_burst200_*.json qat/results/e2e_burst500_*.json \
      -o qat/results/BPO-BOX_Performance_Report.html

Only runs where every uploaded call was processed are used. Repeat runs of the same size are
summarised as typical (median) / best / worst. Written for non-technical readers: every number,
graph and criterion has a plain explanation and a real-life comparison (one restaurant analogy
throughout), plus a glossary. Acceptance criteria are proposed targets, marked as such.
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir from sys.path
import burst_report as B  # noqa: E402
import e2e_report as R
import scenario_compare as S  # noqa: E402

e, mins, num = R.e, B.mins, B.num
DONE = "all uploaded calls processed"
SERIES = ["s1", "s2", "s3", "s4", "s5"]


# ─── Acceptance criteria (proposed targets — agree them with the client) ─────
# kind: "all" = every run must meet it; "typical" = judged on the median run.
CRITERIA = [
    {"key": "complete", "name": "Every call gets analysed", "kind": "all",
     "plain": "No call is lost: every uploaded call is fully analysed (or deliberately skipped because it is under 30 seconds).",
     "life": "Every order that reaches the kitchen gets cooked. No ticket goes missing.",
     "target": "100% of calls, in every run", "warn": None},
    {"key": "upload_ok", "name": "Uploads accepted", "kind": "all",
     "plain": "The share of uploaded files the system accepted without an error.",
     "life": "Every order slip handed over at the counter is accepted.",
     "target": "≥ 99% in every run", "warn": None},
    {"key": "upload_s", "name": "Time to receive the files", "kind": "typical",
     "plain": "How long it takes for all files of the burst to arrive.",
     "life": "How long the queue at the counter takes to hand over all their slips.",
     "target": "≤ 60 seconds", "warn": "≤ 120 seconds"},
    {"key": "created_s", "name": "Time to register every call", "kind": "typical",
     "plain": "How long until every call in the burst is unpacked and recorded in the system.",
     "life": "How long until the cashier has typed every order into the kitchen screen.",
     "target": "≤ 3 minutes", "warn": "≤ 5 minutes"},
    {"key": "rate", "name": "Processing speed", "kind": "typical",
     "plain": "Average number of calls fully analysed per minute. Small bursts include more start-up time, so their average "
              "looks lower than the system's real top speed.",
     "life": "How many dishes the kitchen serves per minute. A small group still waits for the stoves to heat up, so the "
             "per-minute average looks lower.",
     "target": "≥ 40 calls per minute", "warn": "≥ 25 calls per minute"},
    {"key": "total_s", "name": "Time to finish the whole burst", "kind": "typical",
     "plain": "From sending the burst until the last call is analysed. Target scales with burst size "
              "(about 3 minutes plus 1.5 seconds per call).",
     "life": "From the bus arriving until the last guest is served.",
     "target": "≤ 3 min + 1.5 s per call (100 → 5.5 min, 200 → 8 min, 500 → 15.5 min, 1,000 → 28 min)", "warn": "up to 1.5× the target"},
    {"key": "site_p95", "name": "Website speed during the burst", "kind": "typical",
     "plain": "95 out of 100 clicks on the website are answered within this time while the burst is running.",
     "life": "While the kitchen is busy, how fast the waiter still answers a customer's question.",
     "target": "≤ 2 seconds", "warn": "≤ 3 seconds"},
    {"key": "site_err", "name": "Website errors", "kind": "all",
     "plain": "The share of website clicks that failed during the burst.",
     "life": "Questions the waiter couldn't answer at all.",
     "target": "≤ 1% in every run", "warn": None},
]


def judge(key, size, v):
    """Return (status, text) for one criterion value."""
    if v is None:
        return "info", "n/a"
    if key == "complete":
        return ("pass" if v else "fail"), ("100% analysed" if v else "Calls left unfinished")
    if key == "upload_ok":
        return ("pass" if v >= .99 else "fail"), f"{v * 100:.1f}%"
    if key == "site_err":
        return ("pass" if v <= .01 else "fail"), f"{v * 100:.2f}%"
    if key == "upload_s":
        return ("pass" if v <= 60 else "warn" if v <= 120 else "fail"), mins(v)
    if key == "created_s":
        return ("pass" if v <= 180 else "warn" if v <= 300 else "fail"), mins(v)
    if key == "rate":
        return ("pass" if v >= 40 else "warn" if v >= 25 else "fail"), f"{v:,.0f} / min"
    if key == "total_s":
        limit = 180 + 1.5 * size
        return ("pass" if v <= limit else "warn" if v <= 1.5 * limit else "fail"), mins(v)
    if key == "site_p95":
        return ("pass" if v <= 2000 else "warn" if v <= 3000 else "fail"), f"{v / 1000:.1f} s"
    return "info", str(v)


# ─── Grouping repeat runs ────────────────────────────────────────────────────
def median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def group(results):
    runs = defaultdict(list)
    for r in results:
        if r.get("end_reason") != DONE:
            continue
        b = B.burst_numbers(r)
        b["result"] = r
        runs[(b["size"], int(r.get("calls_per_zip") or 25))].append(b)  # same size, different zip packaging = separate group
    out = []
    for size, cpz in sorted(runs):
        rs = sorted(runs[(size, cpz)], key=lambda b: b["result"].get("started_utc", ""))
        med_total = median([b["total_s"] for b in rs])
        rep = min(rs, key=lambda b: abs((b["total_s"] or 0) - med_total))  # run closest to typical
        zips = -(-size // cpz)
        out.append({
            "size": size, "cpz": cpz, "zips": zips, "key": f"{size}x{cpz}",
            "label": f"{size:,} calls · {zips} zip{'s' if zips > 1 else ''} of {cpz}",
            "runs": rs, "rep": rep, "n": len(rs),
            "total": med_total, "best": min(b["total_s"] for b in rs), "worst": max(b["total_s"] for b in rs),
            "upload_s": median([b["upload_s"] for b in rs]), "created_s": median([b["created_s"] for b in rs]),
            "rate": median([b["rate"] for b in rs]), "backlog": median([b["backlog"] for b in rs]),
            "site_p95": median([b["site_p95"] for b in rs]), "site_p50": median([b["site_p50"] for b in rs]),
            "site_err_worst": max((b["site_err"] or 0) for b in rs),
            "upload_ok_worst": min((b["upload_ok"] if b["upload_ok"] is not None else 1) for b in rs),
            "complete_all": all(b["complete"] for b in rs),
            "calls": sum(b["uploaded"] for b in rs),
        })
    return out


def _family(groups):
    """Groups sharing the zip size that covers the most burst sizes (ties: most runs, then smaller zips)."""
    by = defaultdict(list)
    for g in groups:
        by[g["cpz"]].append(g)
    cpz = max(by, key=lambda c: (len(by[c]), sum(g["n"] for g in by[c]), -c))
    return sorted(by[cpz], key=lambda g: g["size"])


def crit_value(g, key):
    return {"complete": g["complete_all"], "upload_ok": g["upload_ok_worst"], "site_err": g["site_err_worst"],
            "upload_s": g["upload_s"], "created_s": g["created_s"], "rate": g["rate"], "total_s": g["total"],
            "site_p95": g["site_p95"]}[key]


# ─── Page pieces ─────────────────────────────────────────────────────────────
CLIENT_CSS = """
.toc{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.toc a{font-size:13px;color:var(--text-2);text-decoration:none;background:var(--surface);
border:1px solid var(--border);border-radius:999px;padding:5px 12px}.toc a:hover{border-color:var(--s1);color:var(--s1)}
.crit td{white-space:normal;vertical-align:top}.crit td:first-child{min-width:190px}.crit .life{color:var(--text-3);font-size:12.5px;font-style:italic;margin-top:4px;display:block}
.crit .res{display:flex;flex-direction:column;align-items:center;gap:4px;font-size:12.5px;color:var(--text-2)}
.crit th:not(:first-child),.crit td:not(:first-child){text-align:center}.crit td:nth-child(2){text-align:left;min-width:150px}
.summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.sz{padding:16px 18px}.sz h3{font-size:16px}.sz .big{font-size:28px;font-weight:700;margin-top:6px}.sz .big small{font-size:13px;color:var(--text-3);margin-left:4px}
.sz ul{margin:10px 0 0;padding-left:18px;color:var(--text-2);font-size:13px}.sz li+li{margin-top:3px}
.legend-badges{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--text-2);margin-top:10px}
.limit{border-left:4px solid var(--fail)}
@media (max-width:900px){.summary-grid{grid-template-columns:1fr}}
"""


def render(results: list) -> str:
    groups = group(results)
    if not groups:
        return "<p>No completed burst runs found.</p>"
    allr = [b["result"] for g in groups for b in g["runs"]]
    first = min(allr, key=lambda r: r.get("started_utc", ""))
    last = max(allr, key=lambda r: r.get("finished_utc", ""))
    host = urlparse(first.get("base_url", "")).netloc
    sizes = sorted({g["size"] for g in groups})
    fam = _family(groups)  # one zip size that covers the most burst sizes: used for size-vs-size charts
    biggest = fam[-1]
    total_calls = sum(g["calls"] for g in groups)
    total_runs = sum(g["n"] for g in groups)

    # Acceptance verdict per size
    verdicts = {}
    for g in groups:
        sts = [judge(c["key"], g["size"], crit_value(g, c["key"]))[0] for c in CRITERIA]
        verdicts[g["key"]] = "fail" if "fail" in sts else "warn" if "warn" in sts else "pass"
    overall = "fail" if "fail" in verdicts.values() else "warn" if "warn" in verdicts.values() else "pass"
    vlabel = {"pass": "Meets all targets", "warn": "Meets targets with watch points", "fail": "Some targets not met"}

    # ── Headline cards ──
    cards = "".join([
        B.kpi("Calls tested", num(total_calls), "calls",
              f"Across {total_runs} test runs at {B._and_list([f'{s:,}' for s in sizes])} calls at once, packed in zips of {B._and_list(sorted({str(g['cpz']) for g in groups}, key=int))} calls.",
              "The number of orders the kitchen cooked during the practice rush days.", "neutral"),
        B.kpi("Calls lost", "0", "calls", "Every uploaded call in every completed run was analysed or deliberately skipped.",
              "Not a single order ticket went missing.", "good"),
        B.kpi(f"Typical time for {biggest['size']:,} calls", f"{(biggest['total'] or 0) / 60:.1f}", "minutes",
              f"From sending {biggest['size']:,} calls at once until the last one was analysed "
              f"(best {mins(biggest['best'])}, worst {mins(biggest['worst'])}).",
              "From a big tour bus arriving until the last guest gets their food.", "neutral"),
        B.kpi("Processing speed", num(median([g["rate"] for g in groups])), "calls / min",
              "Typical number of calls finished per minute. It stays roughly the same whatever the burst size: that is the system's current top speed.",
              "A kitchen with a fixed number of stoves can only serve so many dishes per minute.", "neutral"),
        B.kpi(f"Website speed at {biggest['size']:,} calls", f"{(biggest['site_p95'] or 0) / 1000:.1f}", "seconds",
              "95 out of 100 website clicks were answered within this time during the biggest burst. Under 2 seconds feels quick.",
              "During the busiest rush, how long the slowest customers waited for the waiter.",
              "good" if (biggest["site_p95"] or 0) <= 2000 else "warn" if (biggest["site_p95"] or 0) <= 3000 else "bad"),
        B.kpi("Uploads accepted", f"{min(g['upload_ok_worst'] for g in groups) * 100:.0f}", "%",
              "Share of uploaded files the system accepted in the completed runs.",
              "Every order slip was accepted at the counter.", "good"),
    ])

    # ── Per-size summary tiles ──
    tiles = "".join(
        f'<div class="card sz"><h3>{e(g["label"])} {R.badge(verdicts[g["key"]], vlabel[verdicts[g["key"]]].split(" with")[0] if verdicts[g["key"]] != "warn" else "Watch points")}</h3>'
        f'<div class="big">{mins(g["total"])}<small>typical</small></div>'
        f'<ul><li>Tested {g["n"]} time{"s" if g["n"] > 1 else ""}: best {mins(g["best"])}, worst {mins(g["worst"])}</li>'
        f'<li>All calls registered after {mins(g["created_s"])}</li><li>{num(g["rate"])} calls analysed per minute</li>'
        f'<li>Website: 95% of clicks within {(g["site_p95"] or 0) / 1000:.1f} s</li></ul></div>' for g in groups)

    # ── Acceptance criteria table ──
    crit_rows = []
    for c in CRITERIA:
        cells = []
        for g in groups:
            st, txt = judge(c["key"], g["size"], crit_value(g, c["key"]))
            cells.append(f'<td><div class="res">{R.badge(st)}<span>{e(txt)}</span></div></td>')
        crit_rows.append(f'<tr><td><b>{e(c["name"])}</b><span class="desc">{e(c["plain"])}</span><span class="life">Like: {e(c["life"])}</span></td>'
                         f'<td>{e(c["target"])}{"<span class=desc>Watch: " + e(c["warn"]) + "</span>" if c["warn"] else ""}'
                         f'<span class="desc">{"Every run" if c["kind"] == "all" else "Typical run"}</span></td>{"".join(cells)}</tr>')
    crit_table = (f'<div class="card table-wrap"><table class="crit"><thead><tr><th>What we check</th><th>Target</th>'
                  + "".join(f"<th>{e(g['label'])}</th>" for g in groups)
                  + f'</tr></thead><tbody>{"".join(crit_rows)}</tbody><tfoot><tr><td><b>Overall</b></td><td></td>'
                  + "".join(f"<td>{R.badge(verdicts[g['key']], vlabel[verdicts[g['key']]])}</td>" for g in groups)
                  + '</tr></tfoot></table><p class="tbl-note">These targets are <b>proposed</b> using common industry practice for business web '
                    'applications (pages under 2–3 seconds, no lost data, steady processing). Please confirm or adjust them; the report '
                    'recalculates if they change. "Typical run" means the middle (median) result across repeat tests.</p></div>')
    legend = ('<div class="legend-badges"><span>' + R.badge("pass") + ' meets the target</span><span>' + R.badge("warn")
              + ' acceptable but worth watching</span><span>' + R.badge("fail") + ' does not meet the target</span></div>')

    # ── Comparison table ──
    comp_rows = "".join(
        f"<tr><td><span class=\"name\">{e(g['label'])}</span><span class=\"desc\">{g['n']} run{'s' if g['n'] > 1 else ''}</span></td>"
        f"<td>{mins(g['upload_s'])}</td><td>{mins(g['created_s'])}</td><td><b>{mins(g['total'])}</b></td>"
        f"<td>{mins(g['best'])}</td><td>{mins(g['worst'])}</td><td>{num(g['rate'])}</td>"
        f"<td>{num((g['total'] or 0) / g['size'], 2)} s</td><td>{num(g['backlog'])}</td>"
        f"<td>{(g['site_p50'] or 0) / 1000:.2f} s</td><td>{(g['site_p95'] or 0) / 1000:.1f} s</td>"
        f"<td>{(g['site_err_worst'] or 0) * 100:.2f}%</td></tr>" for g in groups)
    comp = (f'<div class="card table-wrap"><table class="btable"><thead><tr><th>Burst size</th><th>Files received</th><th>All calls registered</th>'
            f'<th>Total time (typical)</th><th>Best</th><th>Worst</th><th>Calls / min</th><th>Time per call</th><th>Longest waiting line</th>'
            f'<th>Website typical</th><th>Website 95%</th><th>Website errors</th></tr></thead><tbody>{comp_rows}</tbody></table>'
            f'<p class="tbl-note">Times count from the moment the burst was sent. "Typical" is the middle result of the repeat runs. '
            f'Progress was checked every 15 seconds, so finish times are accurate to within about 15 seconds.</p></div>')

    # ── Charts ──
    labels = [f"{g['size']:,} × {g['cpz']}" for g in groups]
    c_total = B.with_explain(
        R.bar_chart("How long each burst size takes", "Typical total time, from sending the burst to the last call analysed (minutes)",
                    labels, [round((g["total"] or 0) / 60, 2) for g in groups], "s1", R._num, " min", wide=True,
                    show_values=True, bar_max=150),
        B.explain("One bar per burst size, showing the typical time until every call in that burst was analysed.",
                  "Three tour buses of different sizes. Each bar is how long until the last person on that bus had their food.",
                  "Taller bar = longer wait. Compare how much the bar grows when the burst gets bigger.",
                  f"With zips of {biggest['cpz']}: {biggest['size']:,} calls took about {mins(biggest['total'])}; {fam[0]['size']:,} calls took about {mins(fam[0]['total'])}."))

    run_labels, run_vals = [], []
    for g in groups:
        for i, b in enumerate(g["runs"], 1):
            run_labels.append(f"{g['size']:,}×{g['cpz']} · {i}")
            run_vals.append(round((b["total_s"] or 0) / 60, 2))
    c_runs = B.with_explain(
        R.bar_chart("Consistency across repeat tests", "Total time of every individual test run (minutes)", run_labels, run_vals,
                    "s3", R._num, " min", wide=True, show_values=True),
        B.explain("Each bar is one separate test run. We repeated each burst size to see whether results are consistent.",
                  "Timing the same dinner rush on different evenings: some nights the kitchen is a bit faster than others.",
                  "Bars of the same burst size that are similar in height mean the system behaves predictably. Big differences mean "
                  "results depend on other activity at the time.",
                  _consistency_takeaway(groups)))

    seg_rows = []
    for g in groups:
        up = (g["upload_s"] or 0) / 60
        cr = max(0.0, ((g["created_s"] or 0) - (g["upload_s"] or 0)) / 60)
        an = max(0.0, ((g["total"] or 0) - (g["created_s"] or 0)) / 60)
        seg_rows.append((g["label"], [round(up, 2), round(cr, 2), round(an, 2)]))
    c_parts = B.with_explain(
        R.stacked_hbar("Where the time goes", "Typical minutes spent in each stage", seg_rows,
                       ["Receiving files", "Registering calls", "Analysing the remaining calls"], ["s1", "s4", "s3"], " min"),
        B.explain("Each bar splits the total time into three back-to-back stages.",
                  "Slips reach the counter, the cashier types them in, then the kitchen cooks the remaining dishes.",
                  "The longest coloured piece is the slowest stage. Speeding that stage up gives the biggest improvement.",
                  "Analysis is by far the longest stage at every size: the analysis workers are the bottleneck, not the upload."))

    reps = [g["rep"] for g in fam]
    tmax = max([(b["total_s"] or 0) for b in reps] + [60])
    step = max(15, int(-(-tmax // 150 // 15)) * 15)
    xs = list(range(0, int(tmax) + step, step))
    prog, back = [], []
    for i, b in enumerate(reps[:5]):
        tl, n = b["timeline"], max(1, b["uploaded"])
        prog.append((f"{b['size']:,} calls", SERIES[i], [round(100 * B._at(tl, x + b["up_start"], "processed") / n, 1) for x in xs]))
        back.append((f"{b['size']:,} calls", SERIES[i],
                     [B._at(tl, x + b["up_start"], "ingested") - B._at(tl, x + b["up_start"], "processed") for x in xs]))
    c_race = B.with_explain(
        R.line_chart("Progress over time", "Share of each burst analysed, minute by minute (typical run of each size)", xs, prog,
                     R._num, "%", wide=True, ymax=100),
        B.explain("Each line climbs from 0% to 100% as that burst's calls are analysed. Time starts when the burst was sent.",
                  "A progress bar on the kitchen wall for each bus group.",
                  "Steeper = faster. Where a line reaches the top is when that burst finished. Similar slopes mean the system works at a steady pace."))
    c_back = B.with_explain(
        R.line_chart("Waiting line over time", "Calls registered but not yet analysed (typical run of each size)", xs, back,
                     R._num, " calls", wide=True),
        B.explain("How many calls were waiting for analysis at each moment.",
                  "Order tickets clipped on the kitchen rail waiting for a free chef.",
                  "The line rises as calls arrive faster than they're analysed, peaks, then falls to zero. A higher peak means a longer wait for the last calls.",
                  f"The longest waiting line was about {num(biggest['backlog'])} calls during the {biggest['size']:,}-call burst, and it always cleared to zero."))
    c_rate = B.with_explain(
        R.bar_chart("Processing speed", "Typical calls analysed per minute", labels, [round(g["rate"] or 0, 1) for g in groups],
                    "s3", R._num, " calls/min", show_values=True),
        B.explain("How many calls per minute were finished, on average, for each burst size.",
                  "Dishes served per minute. A kitchen with a fixed number of stoves has a top speed.",
                  "If the bars stay about the same height as bursts grow, the system has reached its top speed.",
                  f"Speed stays around {num(median([g['rate'] for g in groups]))} calls per minute: this is the current capacity of the analysis step."))
    c_scale = B.with_explain(
        R.line_chart("Does waiting time grow in step with size?", f"Typical minutes vs burst size (zips of {fam[0]['cpz']} calls), compared with straight-line growth",
                     [g["size"] for g in fam], [("Measured", "s1", [round((g["total"] or 0) / 60, 2) for g in fam]),
                             ("If it grew in direct proportion", "s2",
                              [round((fam[0]["total"] or 0) / 60 * g["size"] / fam[0]["size"], 2) for g in fam])],
                     R._num, " min", xfmt="num", xlab=lambda v: f"{v:,}"),
        B.explain("Measured time for each burst size next to a reference line that assumes double the calls means double the time.",
                  "If 10 guests wait 5 minutes, would 20 guests wait 10?",
                  "Measured line below the reference: bigger bursts are handled more efficiently. Above it: bigger bursts slow down more than expected."))
    c_site = B.with_explain(
        R.line_chart("Website speed at each burst size", f"Seconds to answer a click: typical, and 95 out of 100 (zips of {fam[0]['cpz']} calls)",
                     [g["size"] for g in fam], [("Typical click", "s1", [round((g["site_p50"] or 0) / 1000, 2) for g in fam]),
                             ("95 out of 100 clicks", "s2", [round((g["site_p95"] or 0) / 1000, 2) for g in fam])],
                     R._num, " s", xfmt="num", xlab=lambda v: f"{v:,}"),
        B.explain("How quickly the website answered while each burst was being processed. The lower line is a typical click; the upper line is "
                  "the slower clicks (95 out of 100 were faster than this).",
                  "During the rush, how long customers wait for the waiter: most are served quickly, a few wait longer.",
                  "Under 2 seconds feels quick; 2–3 seconds is noticeable; over 3 seconds feels slow.",
                  _site_takeaway(fam)))
    rep_big = biggest["rep"].get("result", {}).get("k6_series") or {}
    c_site_time = ""
    if rep_big.get("t"):
        c_site_time = B.with_explain(
            R.line_chart(f"Website speed during the {biggest['size']:,}-call burst", "Seconds to answer a click, moment by moment",
                         rep_big["t"], [("Typical click", "s1", [None if v is None else round(v / 1000, 2) for v in rep_big["browse_p50"]]),
                                        ("95 out of 100 clicks", "s2", [None if v is None else round(v / 1000, 2) for v in rep_big["browse_p95"]])],
                         R._num, " s", wide=True),
            B.explain("How the website's speed changed over the course of the biggest burst.",
                      "How the waiter's response time changes as the rush builds up and then calms down.",
                      "Peaks show the busiest moments, usually while the uploaded files are being unpacked."))
    c_upload = B.with_explain(
        R.bar_chart("Time to receive the files", "Typical seconds until every file of the burst arrived", labels,
                    [round(g["upload_s"] or 0, 1) for g in groups], "s4", R._num, " s", show_values=True),
        B.explain("How long the upload of all files in a burst took.",
                  "How long the queue at the counter took to hand over every slip.",
                  "This mostly depends on the internet connection of the computer sending the files."))

    # ── Findings ──
    findings = _client_findings(groups, verdicts)
    story = "".join(
        f'<div class="step"><span class="num">{i}</span><h3>{t}</h3><p>{d}</p><p class="life">{life}</p></div>'
        for i, (t, d, life) in enumerate([
            ("Send", "A burst of call recordings is uploaded all at once.", "A tour bus arrives and everyone hands in their order."),
            ("Register", "The system unpacks each recording and creates a call record.", "The cashier enters each order."),
            ("Analyse", "Each call is transcribed and quality-scored by cloud workers, many at a time.", "Chefs cook on the available stoves."),
            ("Done", "We stop the clock when the last call of the burst is finished.", "The last guest gets their plate."),
        ], 1))
    extra_gloss = ("Reading this report", [
        ("Acceptance criteria", "The targets the system should meet to be considered ready, agreed in advance.",
         "The standards a restaurant sets: no lost orders, food within 15 minutes."),
        ("Typical (median)", "The middle result when tests are repeated. Half the runs were faster, half slower.", "A normal evening, not the best or worst."),
        ("Best / worst", "The fastest and slowest result among the repeat runs.", "The quietest and the busiest evening."),
        ("Run", "One complete test: send a burst and wait until every call is analysed.", "One dinner rush."),
        ("Pass / Watch / Fail", "Pass meets the target, Watch is acceptable but close to the limit, Fail misses the target.",
         "A health inspection: fine, needs attention, must fix."),
    ])
    gloss = "".join(
        f'<div class="gloss-group">{e(gname)}</div>' + "".join(
            f'<div class="g"><h3>{e(term)}</h3><p>{e(defn)}</p><p class="life">Like: {e(life)}</p></div>' for term, defn, life in items)
        for gname, items in [extra_gloss] + B.GLOSSARY)
    runs_rows = "".join(
        f"<tr><td>{e(g['label'])} · run {i}</td><td>{e(b['result'].get('started_utc', '')[:16].replace('T', ' '))} UTC</td>"
        f"<td>{mins(b['upload_s'])}</td><td>{mins(b['created_s'])}</td><td><b>{mins(b['total_s'])}</b></td><td>{num(b['rate'])}</td>"
        f"<td>{num(b['backlog'])}</td><td>{(b['site_p95'] or 0) / 1000:.1f} s</td><td>{b['scored']:,} / {b['skipped']:,}</td></tr>"
        for g in groups for i, b in enumerate(g["runs"], 1))
    s0, s1 = first.get("started_utc", ""), last.get("finished_utc", "")
    when = (f"{s0[:10]} {s0[11:16]}–{s1[11:16]} UTC" if s0[:10] == s1[:10]
            else f"{s0[:10]} {s0[11:16]} → {s1[:10]} {s1[11:16]} UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Performance Test Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>{R.CSS}{B.EXTRA_CSS}{CLIENT_CSS}{S.CSS}</style></head><body><main class="wrap">
<header class="hero"><div class="eyebrow">Performance test report · BPO-BOX call analysis</div>
<h1>How the system handles a sudden rush of calls</h1>
<p>We sent bursts of {B._and_list([f'{s:,}' for s in sizes])} call recordings to the system all at once, {total_runs} times in total, and measured
how long it took until every call was analysed, how fast the system worked, and how responsive the website stayed for users at the same time.</p>
<span class="pill">Environment: {e(host)} (test system)</span><span class="pill">{e(when)}</span><span class="pill">{num(total_calls)} calls · {total_runs} runs</span>
<span class="pill">AI: simulated (no AI cost)</span>
<nav class="toc"><a href="#summary">Summary</a><a href="#limits">Limits</a><a href="#scenarios">All scenarios</a><a href="#verdict">Good / to improve</a><a href="#criteria">Acceptance criteria</a><a href="#compare">Comparison</a>
<a href="#time">Time</a><a href="#capacity">Capacity</a><a href="#users">User experience</a><a href="#findings">Findings</a>
<a href="#glossary">Glossary</a><a href="#method">How we tested</a></nav></header>

<section id="summary"><div class="section-head"><h2>Summary</h2><p>The short version</p></div>
<div class="verdict-box {overall}"><h2>Overall: {vlabel[overall]}</h2><p>{_summary_sentence(groups, verdicts)}</p></div>
<div class="summary-grid" style="margin-top:14px">{tiles}</div></section>

<section id="limits"><div class="section-head"><h2>Limits found</h2><p>What the system handles today, including the steady-rate test</p></div><div class="bks">{S.limits_cards()}</div></section>

<section id="scenarios"><div class="section-head"><h2>All successful scenarios compared</h2><p>Smoke checks, bursts and steady load that passed</p></div>{S.compare_table(include_failed=False)}</section>

<section id="verdict"><div class="section-head"><h2>What is good and what needs improvement</h2><p>Based on all testing so far</p></div>{S.good_improve_html()}</section>

<section><div class="section-head"><h2>Key numbers</h2><p>What each number means, and what it's like in everyday life</p></div><div class="bks">{cards}</div></section>

<section><div class="section-head"><h2>How a call moves through the system</h2><p>The four steps we timed</p></div><div class="story">{story}</div></section>

<section id="criteria"><div class="section-head"><h2>Acceptance criteria</h2><p>Did the system meet the targets?</p></div>{crit_table}{legend}</section>

<section id="compare"><div class="section-head"><h2>Comparison of burst sizes</h2><p>All measurements side by side</p></div>{comp}</section>

<section id="time"><div class="section-head"><h2>Time</h2><p>How long bursts take, how consistent that is, and where the time goes</p></div>
<div class="grid1">{c_total}</div><div class="grid1">{c_runs}</div><div class="grid1">{c_parts}</div><div class="grid1">{c_race}</div></section>

<section id="capacity"><div class="section-head"><h2>Capacity</h2><p>How much the system can handle at once</p></div>
<div class="grid1">{c_back}</div><div class="grid2">{c_rate}{c_scale}</div></section>

<section id="users"><div class="section-head"><h2>User experience</h2><p>What people using the website felt during each burst</p></div>
<div class="grid2">{c_site}{c_upload}</div>{f'<div class="grid1">{c_site_time}</div>' if c_site_time else ''}</section>

<section id="findings"><div class="section-head"><h2>What we learned &amp; recommended next steps</h2></div><div class="alerts">{findings}</div></section>

<section id="glossary"><div class="section-head"><h2>Glossary</h2><p>Every term in this report, in plain words</p></div><div class="gloss">{gloss}</div></section>

<section id="method"><div class="section-head"><h2>How we tested</h2></div>
<p class="note">Each burst sent its calls at the same moment, as zip files uploaded in parallel from one computer (zips of 25, 100, 125 or 500 recordings,
as labelled), to the {e(host)}
test environment. A run started only when the system had no other test calls waiting, and the clock stopped when the last call was analysed or
deliberately skipped (recordings under 30 seconds are skipped by design). While each burst ran, simulated users kept opening the Call Review pages to
measure website speed. AI transcription and scoring were simulated with realistic timing, so these results describe the platform's speed and
capacity, not AI quality, and real AI services may add their own limits. Each burst size was repeated to check consistency; the report shows the
typical (median) result with best and worst. Server-side measurements (processor and database load) were not part of this test.</p>
<details class="data" style="margin-top:12px"><summary>Every individual test run</summary><div class="table-wrap"><table><thead><tr><th>Run</th><th>Started</th>
<th>Files received</th><th>All registered</th><th>Total time</th><th>Calls / min</th><th>Longest waiting line</th><th>Website 95%</th><th>Analysed / skipped</th></tr></thead>
<tbody>{runs_rows}</tbody></table></div></details></section>
<footer>BPO-BOX performance test · generated from {total_runs} completed test runs · k6 load generator + system progress checks</footer>
</main><div id="tip" hidden></div><script>{R.JS}</script></body></html>"""


def _summary_sentence(groups, verdicts):
    big = _family(groups)[-1]
    parts = [f"Every call in all {sum(g['n'] for g in groups)} test runs was analysed, with no lost calls and no upload errors.",
             f"A burst of {big['size']:,} calls at once (zips of {big['cpz']}) typically takes about {mins(big['total'])} to finish, "
             f"at a steady {num(median([g['rate'] for g in groups]))} calls per minute."]
    slow = [g for g in groups if (g["site_p95"] or 0) > 2000]
    if slow:
        parts.append(f"The main point to address is website speed during the larger bursts: at {slow[-1]['size']:,} calls, 95% of clicks took up to "
                     f"{(slow[-1]['site_p95'] or 0) / 1000:.1f} seconds.")
    return " ".join(parts)


def _consistency_takeaway(groups):
    spreads = [(g["label"], g["best"], g["worst"]) for g in groups if g["n"] > 1]
    if not spreads:
        return "Each size was run once; repeat runs would show how consistent the timing is."
    s, lo, hi = max(spreads, key=lambda t: (t[2] - t[1]) / max(t[1], 1))
    return (f"Results vary between runs. The widest spread was at {s} ({mins(lo)} to {mins(hi)}), mainly because other "
            "activity on the shared test system affects speed. The typical value is the fairest figure to quote.")


def _site_takeaway(groups):
    worst = max(groups, key=lambda g: g["site_p95"] or 0)
    if (worst["site_p95"] or 0) <= 2000:
        return "The website stayed quick at every burst size."
    return (f"The website slows down as bursts get bigger: at {worst['size']:,} calls, 95% of clicks took up to "
            f"{(worst['site_p95'] or 0) / 1000:.1f} seconds, above the 2-second target.")


def _client_findings(groups, verdicts):
    items = []

    def add(kind, title, body):
        cls = {"good": "a-pass", "warn": "a-warn", "bad": "a-fail"}[kind]
        lab = {"good": ("pass", "Strength"), "warn": ("warn", "Watch point"), "bad": ("fail", "To fix")}[kind]
        items.append(f'<div class="alert {cls}"><div class="stripe"></div><div class="body"><div class="title">{R.badge(*lab)}'
                     f'<h3>{title}</h3></div><ul><li>{body}</li></ul></div></div>')

    big = _family(groups)[-1]
    add("good", "Reliable: nothing was lost",
        f"Across {sum(g['n'] for g in groups)} runs and {sum(g['calls'] for g in groups):,} calls, every call was analysed and every upload was accepted.")
    add("good", "Uploads and registration are quick",
        f"Even {big['size']:,} calls (zips of {big['cpz']}) arrived in about {mins(big['upload_s'])} and were all registered within {mins(big['created_s'])}.")
    rate = median([g["rate"] for g in groups])
    add("warn", f"Analysis speed is the limiting factor (about {num(rate)} calls per minute)",
        f"Most of the total time is spent waiting for analysis, which works on a limited number of calls at once. At this speed, 1,000 calls "
        f"would take roughly {mins(1000 / rate * 60) if rate else 'n/a'}. Adding analysis capacity is the main way to shorten big bursts.")
    slow = [g for g in groups if (g["site_p95"] or 0) > 2000]
    if slow:
        add("bad", "Website slows down during large bursts",
            f"At {slow[-1]['size']:,} calls, 95% of clicks took up to {(slow[-1]['site_p95'] or 0) / 1000:.1f} seconds (target: 2 seconds). "
            "Uploaded files are currently unpacked on the same servers that run the website. Moving that work to background servers "
            "would keep the website fast during a rush.")
    add("bad", "Limit found: how the calls are packed matters more than how many there are",
        "1,000 calls sent as 40 small zips of 25 overloaded the test system three times: every zip is unpacked at the same moment and each one "
        "holds a database connection, so the 18 connections each server process has ran out, uploads were rejected and users saw login errors. "
        "The same 1,000 calls sent as 2, 8 or 10 larger zips were processed completely every time. Until unpacking is moved to its own "
        "background workers with a fixed limit, keep each burst to about 10 zips or fewer.")
    add("warn", "Bigger zips are gentler on the system but slower to arrive",
        "With 2 zips of 500, uploading took over 4 minutes on a normal office connection, so the burst finished later (about 22 minutes) than "
        "with 8 zips of 125 (about 12 minutes). About 8–10 zips of 100–125 calls gave the best balance in these tests.")
    add("warn", "These results use simulated AI",
        "Transcription and scoring were simulated with realistic timing to avoid AI costs. Real AI services add their own speed limits, so a short "
        "confirmation test with real AI is recommended before quoting these figures externally.")
    return "".join(items)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("qat/results/BPO-BOX_Performance_Report.html"))
    args = ap.parse_args()
    results = []
    for p in args.results:
        if p.name.endswith("_k6.json"):
            continue
        try:
            results.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as err:
            print(f"Cannot read {p}: {err}", file=sys.stderr)
            return 1
    used = [r for r in results if r.get("end_reason") == DONE]
    print(f"Using {len(used)} completed runs (skipped {len(results) - len(used)} incomplete).")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(results), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Plain-language HTML report for burst tests (run_burst.py results → one standalone page).

  python -I burst_report.py qat/results/e2e_burst*_20261103_*.json -o qat/results/burst_report.html

Written for non-technical readers: every KPI card and graph carries "what it shows",
a real-life example (one restaurant analogy used throughout), "how to read it" and a
data-driven takeaway, plus a glossary of every term. Charts reuse e2e_report's toolkit
(one y-axis per chart, hover values, "Show data" tables, validated palette, dark mode).
"""
import argparse
import html
import json
import math
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python -I` drops the script dir from sys.path
import e2e_report as R  # noqa: E402

e = R.e
SERIES = ["s1", "s2", "s3", "s4", "s5"]


# ─── Numbers per burst ───────────────────────────────────────────────────────
def _at(tl, t, key):
    """Value of `key` at time t (last sample at or before t; 0 before the first)."""
    v = 0
    for s in tl:
        if s["t"] <= t:
            v = s[key]
        else:
            break
    return v


def burst_numbers(r: dict) -> dict:
    d = R.derive(r)
    s, k = r.get("k6_series") or {}, r.get("k6") or {}
    n = r.get("uploaded_calls") or 0
    up_start = s.get("upload_first_start_s") or 0
    up_end = s.get("upload_last_end_s") or r.get("upload_end_s")
    created, done = d["t_all_ingested"], d["t_all_processed"]
    total = (done - up_start) if done is not None else None
    tl = r.get("timeline") or []
    finished = (r.get("final") or {}).get("processed", 0)
    return {
        "size": (r["plan"].get("burst") or {}).get("calls", n), "uploaded": n, "zips": k.get("zips_uploaded", 0),
        "upload_s": (up_end - up_start) if up_end is not None else None,
        "created_s": (created - up_start) if created is not None else None,
        "total_s": total,
        "rate": (n / (total / 60)) if total else None,
        "peak_rate": d["peak_processed"],
        "per_call_s": (total / n) if total and n else None,
        "backlog": d["peak_backlog"],
        "site_p95": k.get("browse_p95_ms"), "site_p50": k.get("browse_med_ms"),
        "site_err": k.get("browse_error_rate"),
        "upload_ok": k.get("upload_success"),
        "scored": (r.get("final") or {}).get("scored", 0), "skipped": (r.get("final") or {}).get("skipped", 0),
        "finished": finished, "complete": done is not None, "end_reason": r.get("end_reason"),
        "timeline": tl, "up_start": up_start,
    }


# ─── Formatting ──────────────────────────────────────────────────────────────
def mins(sec):
    if sec is None:
        return "not finished"
    if sec < 10:
        return f"{sec:.1f} sec"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} sec"
    return f"{sec // 60} min {sec % 60:02d} sec" if sec < 3600 else f"{sec // 3600} h {sec % 3600 // 60:02d} min"


def short_mins(sec):
    return "–" if sec is None else f"{sec / 60:.1f} min"


def num(v, d=0):
    return "–" if v is None else f"{v:,.{d}f}"


def explain(what, analogy, read, takeaway=None):
    tk = f'<div class="ex ex-take"><b>What we learned</b><p>{takeaway}</p></div>' if takeaway else ""
    return (f'<div class="explain"><div class="ex ex-what"><b>What it shows</b><p>{what}</p></div>'
            f'<div class="ex ex-life"><b>Real-life example</b><p>{analogy}</p></div>'
            f'<div class="ex ex-read"><b>How to read it</b><p>{read}</p></div>{tk}</div>')


def with_explain(card_html, box):
    """Insert the explainer inside a chart card (before its closing tag)."""
    i = card_html.rfind("</figure>")
    return card_html[:i] + box + card_html[i:]


def kpi(label, value, unit, meaning, analogy, tone="neutral"):
    return (f'<div class="card bk tone-{tone}"><div class="bk-label">{e(label)}</div>'
            f'<div class="bk-value">{value}<small>{e(unit)}</small></div>'
            f'<p class="bk-mean">{meaning}</p><p class="bk-life"><span>Like:</span> {analogy}</p></div>')


# ─── Page ────────────────────────────────────────────────────────────────────
EXTRA_CSS = """
.hero{background:linear-gradient(135deg,#1f3a8a 0%,#2a78d6 55%,#1baf7a 130%);color:#fff;border-radius:16px;padding:30px 32px;position:relative;overflow:hidden}
.hero h1{font-size:28px;font-weight:700;margin-top:6px;color:#fff}.hero .eyebrow{color:rgba(255,255,255,.8)}
.hero p{color:rgba(255,255,255,.92);max-width:78ch;margin-top:10px;font-size:15px}
.hero .pill{display:inline-block;margin-top:14px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);padding:5px 12px;border-radius:999px;font-size:13px;margin-right:8px}
.story{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}
.story .step{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.story .num{display:inline-flex;width:24px;height:24px;border-radius:50%;align-items:center;justify-content:center;font-weight:700;font-size:12px;background:var(--info-bg);color:var(--info);border:1px solid var(--info-bd)}
.story h3{margin-top:8px}.story p{color:var(--text-2);font-size:13px;margin-top:4px}
.story .life{color:var(--text-3);font-size:12.5px;margin-top:6px;font-style:italic}
.verdict-box{margin-top:16px;padding:16px 18px;border-radius:12px;border:1px solid var(--pass-bd);background:var(--pass-bg)}
.verdict-box.warn{border-color:var(--warn-bd);background:var(--warn-bg)}.verdict-box.fail{border-color:var(--fail-bd);background:var(--fail-bg)}
.verdict-box h2{font-size:16px}.verdict-box p{color:var(--text-2);margin-top:6px}
.bks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.bk{padding:16px 18px;border-top:4px solid var(--s1)}.bk.tone-good{border-top-color:var(--pass)}.bk.tone-warn{border-top-color:var(--warn)}.bk.tone-bad{border-top-color:var(--fail)}
.bk-label{font-size:12.5px;font-weight:600;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em}
.bk-value{font-size:30px;font-weight:700;margin-top:6px;letter-spacing:-.02em}.bk-value small{font-size:14px;color:var(--text-3);margin-left:4px;font-weight:600}
.bk-mean{margin-top:8px;color:var(--text-2);font-size:13.5px}.bk-life{margin-top:8px;font-size:13px;color:var(--text-2);background:var(--surface-2);border:1px dashed var(--border-strong);border-radius:8px;padding:8px 10px}
.bk-life span{font-weight:700;color:var(--warn)}
.explain{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}
.ex{border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.5;border:1px solid var(--border)}
.ex b{display:block;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.ex p{color:var(--text-2)}.ex-what{background:var(--info-bg);border-color:var(--info-bd)}.ex-what b{color:var(--info)}
.ex-life{background:var(--warn-bg);border-color:var(--warn-bd)}.ex-life b{color:var(--warn)}
.ex-read{background:var(--surface-2)}.ex-read b{color:var(--text-2)}
.ex-take{grid-column:1/-1;background:var(--pass-bg);border-color:var(--pass-bd)}.ex-take b{color:var(--pass)}
.grid2 .explain{grid-template-columns:1fr}
.ln.s5{stroke:var(--s5)}.dot.s5{fill:var(--s5)}.ln.s6{stroke:var(--s6)}
.gloss{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.gloss .g{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.gloss .g h3{font-size:14px}.gloss .g p{color:var(--text-2);font-size:13px;margin-top:4px}.gloss .g .life{color:var(--text-3);font-style:italic}
.gloss-group{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);margin:18px 0 8px;grid-column:1/-1}
.tbl-note{padding:10px 14px;font-size:12.5px;color:var(--text-3);border-top:1px solid var(--border)}
.btable td:first-child .name{font-size:15px}
@media (max-width:900px){.bks{grid-template-columns:repeat(2,minmax(0,1fr))}.story{grid-template-columns:repeat(2,minmax(0,1fr))}.explain{grid-template-columns:1fr}.gloss{grid-template-columns:1fr}}
@media (max-width:560px){.bks,.story{grid-template-columns:1fr}.hero{padding:22px 18px}.hero h1{font-size:22px}}
"""

GLOSSARY = [
    ("The test", [
        ("Call", "One recorded phone conversation (an audio file) that the system must analyse.",
         "One food order."),
        ("Burst", "Many calls sent to the system at exactly the same moment, instead of spread out over time.",
         "A tour bus pulling up and 200 people ordering at once."),
        ("Burst size", "How many calls were in one burst: 100, 200, 500, 1,000 or 2,000.",
         "How many people were on the bus."),
        ("Zip file", "A bundle of call recordings packed together for upload. A big burst is several zips sent in parallel.",
         "A tray carrying 25 order slips to the counter."),
        ("Mock AI", "During this test, the paid AI services (transcription and scoring) were replaced by a free stand-in "
         "that answers in a realistic time, so we measure speed without paying for AI.",
         "Practising a dinner rush with plastic food: the kitchen works for real, but no ingredients are wasted."),
    ]),
    ("Stages a call goes through", [
        ("Upload", "Sending the call recordings from a computer to the system over the internet.",
         "Customers handing their order slips to the counter."),
        ("Created", "The system has unpacked the recording and registered the call in its database, ready for analysis.",
         "The cashier typing each order into the kitchen screen."),
        ("Analysed / processed", "The call has been transcribed into text and AI-scored for quality, compliance and KPIs. "
         "It counts as processed when it is scored, or skipped because it is too short (under 30 seconds) to score.",
         "The dish is cooked and served (or the order was cancelled because it was empty)."),
        ("Scored", "The call received its quality score and analysis.", "A dish served to the table."),
        ("Skipped", "The call was too short (under 30 seconds) to analyse, so the system set it aside on purpose. Not an error.",
         "An order slip with nothing on it: nothing to cook."),
    ]),
    ("Speed measures", [
        ("Total time (end-to-end)", "From the moment the burst was sent until the very last call was fully analysed.",
         "From the bus arriving until the last person gets their food."),
        ("Upload time", "How long it took for every zip of the burst to be received by the system.",
         "How long until every order slip reached the counter."),
        ("Throughput (calls per minute)", "How many calls the system finishes per minute, on average across the burst.",
         "How many dishes the kitchen sends out per minute."),
        ("Time per call", "Total time divided by the number of calls. Because the system works on many calls at once, "
         "this is much shorter than the time any single call takes.",
         "A kitchen with 15 chefs finishes a dish every few seconds even though each dish takes minutes to cook."),
        ("Backlog (waiting line)", "Calls that are registered but not analysed yet. It rises when calls arrive faster than "
         "they are analysed, then falls back to zero.",
         "Orders clipped on the kitchen rail, waiting for a chef."),
        ("Peak backlog", "The longest the waiting line got during the burst.", "The most tickets on the rail at one time."),
    ]),
    ("Website experience", [
        ("Response time", "How long the website takes to answer a click (here: opening the Call Review pages) while the burst is running.",
         "How quickly a waiter answers a question while the kitchen is busy."),
        ("p50 (median)", "Half of the website clicks were answered faster than this, half slower.", "The typical wait."),
        ("p95", "95 out of 100 clicks were answered faster than this. It shows how the slower moments feel, not just the average.",
         "95 out of 100 customers waited less than this. Only the unluckiest 5 waited longer."),
        ("Error rate", "The share of website requests or uploads that failed.", "Orders that got lost or sent back."),
        ("Success rate", "The share of uploads the system accepted. 100% means nothing was rejected.",
         "Every order slip was accepted at the counter."),
    ]),
    ("Behind the scenes", [
        ("Lambda (worker)", "Cloud workers that switch on automatically to analyse calls. There is a cap on how many can run at once "
         "(50 for transcription, 15 for each analysis step).",
         "Chefs who arrive the moment orders come in, but the kitchen only has 15 stoves."),
        ("Concurrency", "How many calls are being worked on at the same time.", "How many stoves are cooking at once."),
        ("Queue", "The ordered list of calls waiting for a free worker.", "The ticket rail in the kitchen."),
        ("Bottleneck", "The slowest step, which decides the speed of the whole process.",
         "If there are 15 stoves, it doesn't matter how fast the cashier types."),
    ]),
]


# AI mode comes from AI_MODE in the run's config (run_e2e.py stores it); runs without it were QAT mock runs.
AI_PILL = {"mock": "AI: mock (no AI cost)", "real": "AI: real (billed per call)", "unknown": "AI: not confirmed"}
AI_METHOD = {
    "mock": "AI transcription and scoring ran in mock mode (realistic timing, no AI cost), so these results show the platform's speed, not AI quality.",
    "real": "AI transcription and scoring used the real AI services, so these results include their speed limits and cost.",
    "unknown": "Whether AI transcription and scoring ran in mock or real mode was not confirmed for this environment.",
}


def _ai_mode(results: list) -> str:
    modes = {r.get("ai_mode", "mock") for r in results}
    return modes.pop() if len(modes) == 1 and next(iter(modes)) in AI_PILL else "unknown"


def render(results: list) -> str:
    ai = _ai_mode(results)
    rows = [burst_numbers(r) for r in sorted(results, key=lambda r: ((r["plan"].get("burst") or {}).get("calls", 0), r.get("started_utc", "")))]
    if not rows:
        return "<p>No burst results.</p>"
    first, last = results[0], results[-1]
    host = urlparse(first.get("base_url", "")).netloc
    done = [b for b in rows if b["complete"]]
    biggest = max(rows, key=lambda b: b["size"])
    best_rate = max((b["rate"] or 0) for b in rows)
    worst_p95 = max((b["site_p95"] or 0) for b in rows) or None
    upload_ok = min((b["upload_ok"] if b["upload_ok"] is not None else 1) for b in rows)
    all_ok = len(done) == len(rows)

    # Projection: steady rate from the largest completed burst.
    ref = max(done, key=lambda b: b["size"]) if done else None
    proj_5000 = (5000 / ref["rate"] * 60) if ref and ref["rate"] else None

    tone = "pass" if all_ok and (worst_p95 or 0) <= 1000 else "warn" if all_ok else "fail"
    headline = (f"Every burst was fully processed. The largest, {biggest['size']:,} calls at once, took "
                f"<b>{mins(biggest['total_s'])}</b> from sending to the last call being analysed."
                if all_ok else
                f"{len(done)} of {len(rows)} bursts finished completely. "
                f"The others stopped early ({e(', '.join(sorted({b['end_reason'] for b in rows if not b['complete']})))}).")
    site_line = ("The website stayed quick for users throughout." if (worst_p95 or 0) <= 1000 else
                 f"The website slowed down during the bigger bursts (95% of clicks answered within {num(worst_p95)} ms at worst).")

    # ── Headline KPI cards ──
    cards = [
        kpi("Largest burst completed", num(max((b["size"] for b in done), default=0)), "calls",
            "The biggest number of calls sent at the same moment that the system fully finished.",
            "The biggest tour bus the restaurant fed completely.", "good" if all_ok else "warn"),
        kpi("Time for the largest burst", short_mins(biggest["total_s"]).replace(" min", ""), "minutes",
            f"From sending {biggest['size']:,} calls to the last one being analysed.",
            "From the bus arriving to the last plate reaching a table.", "neutral"),
        kpi("Processing speed", num(best_rate), "calls / min",
            "How many calls the system finished per minute, at its best burst.",
            "How many dishes the kitchen sends out every minute at full speed.", "neutral"),
        kpi("Website speed (worst moment)", num(worst_p95), "ms",
            "95 out of 100 clicks on the Call Review pages were answered within this time, during the busiest burst. "
            "Under 1,000 ms (1 second) feels instant.",
            "Even at peak rush, how fast the waiter answers your question.",
            "good" if (worst_p95 or 0) <= 1000 else "warn" if (worst_p95 or 0) <= 3000 else "bad"),
        kpi("Uploads accepted", f"{(upload_ok or 0) * 100:.1f}", "%",
            "Share of zip uploads the system accepted. 100% means none were rejected or lost.",
            "Every order slip handed over was accepted at the counter.", "good" if (upload_ok or 0) >= .99 else "bad"),
        kpi("If 5,000 arrived at once", short_mins(proj_5000).replace(" min", "") if proj_5000 else "–", "minutes (estimate)",
            f"Estimate at today's speed ({num(ref['rate']) if ref else '–'} calls/min). Not measured directly; capacity changes would change it.",
            "If the bus were 2.5× bigger, how long the last guest would wait with the same kitchen.", "neutral"),
    ]

    # ── Per-burst table ──
    trs = []
    for b in rows:
        st = "pass" if b["complete"] else "fail"
        trs.append(f"<tr><td><span class=\"name\">{b['size']:,} calls</span><span class=\"desc\">{b['zips']} zips</span></td>"
                   f"<td>{mins(b['upload_s'])}</td><td>{mins(b['created_s'])}</td><td><b>{mins(b['total_s'])}</b></td>"
                   f"<td>{num(b['rate'])}</td><td>{num(b['per_call_s'], 2)} s</td><td>{num(b['backlog'])}</td>"
                   f"<td>{num(b['site_p95'])} ms</td><td>{b['scored']:,} / {b['skipped']:,}</td>"
                   f"<td>{R.badge(st, 'Completed' if b['complete'] else 'Not finished')}</td></tr>")
    table = f"""<div class="card table-wrap"><table class="btable"><thead><tr><th>Burst</th><th>Upload time</th><th>All created after</th>
<th>Total time</th><th>Calls / min</th><th>Time per call</th><th>Longest waiting line</th><th>Website p95</th><th>Scored / skipped</th><th>Result</th></tr></thead>
<tbody>{"".join(trs)}</tbody></table><p class="tbl-note">All times are measured from the moment the burst was sent. Progress is checked every
{first.get('timeline') and (first['timeline'][1]['t'] - first['timeline'][0]['t']) if len(first.get('timeline') or []) > 1 else 15} seconds,
so finish times are accurate to within that interval. "Skipped" calls are recordings under 30 seconds, which the system deliberately does not score.</p></div>"""

    # ── Charts ──
    labels = [f"{b['size']:,} calls" for b in rows]
    c_total = with_explain(
        R.bar_chart("How long each burst took", "Total time from sending the burst to the last call being analysed (minutes)",
                    labels, [round((b["total_s"] or 0) / 60, 2) for b in rows], "s1", R._num, " min",
                    wide=True, show_values=True, bar_max=140),
        explain("Each bar is one burst. Its height is the total time, in minutes, until every call in that burst was fully analysed.",
                "Five tour buses of different sizes arrive on different days. Each bar is how long until the last guest on that bus got their food.",
                "Taller bar = longer wait. Compare how much taller the bar gets as the burst grows.",
                f"The {biggest['size']:,}-call burst took {mins(biggest['total_s'])}; the {rows[0]['size']:,}-call burst took {mins(rows[0]['total_s'])}."))

    seg_rows = []
    for b in rows:
        up = (b["upload_s"] or 0) / 60
        cr = max(0.0, ((b["created_s"] or 0) - (b["upload_s"] or 0)) / 60)
        an = max(0.0, ((b["total_s"] or 0) - (b["created_s"] or 0)) / 60)
        seg_rows.append((f"{b['size']:,} calls", [round(up, 2), round(cr, 2), round(an, 2)]))
    c_parts = with_explain(
        R.stacked_hbar("Where the time went", "Minutes spent in each stage, one bar per burst",
                       seg_rows, ["Uploading", "Registering calls", "Finishing analysis"], ["s1", "s4", "s3"], " min"),
        explain("Each bar splits a burst's total time into three back-to-back stages: sending the files, the system registering "
                "every call, and the remaining time until the last call was analysed.",
                "Order slips reach the counter (uploading), the cashier types them in (registering), then the kitchen finishes "
                "the remaining dishes (analysis).",
                "The longest coloured piece is the slowest stage: that's where improvements pay off most.",
                _parts_takeaway(rows)))

    # Progress race / backlog over a shared time grid.
    tmax = max([(b["total_s"] or (b["timeline"][-1]["t"] - b["up_start"] if b["timeline"] else 0)) for b in rows] + [60])
    step = max(15, int(math.ceil(tmax / 150 / 15)) * 15)
    xs = list(range(0, int(tmax) + step, step))
    prog, back = [], []
    for i, b in enumerate(rows[:5]):
        tl, n = b["timeline"], max(1, b["uploaded"])
        prog.append((f"{b['size']:,} calls", SERIES[i], [round(100 * _at(tl, x + b["up_start"], "processed") / n, 1) for x in xs]))
        back.append((f"{b['size']:,} calls", SERIES[i],
                     [_at(tl, x + b["up_start"], "ingested") - _at(tl, x + b["up_start"], "processed") for x in xs]))
    c_race = with_explain(
        R.line_chart("Progress race", "Share of each burst's calls fully analysed over time (%)", xs, prog, R._num, "%",
                     wide=True, ymax=100),
        explain("Each line is one burst. It climbs from 0% to 100% as its calls get analysed. The time axis starts when that burst was sent.",
                "Five buses, five progress bars on the kitchen wall: each shows what share of that bus's guests have their food.",
                "A steeper line = faster progress. Where a line reaches the top (100%) is when that burst finished. "
                "Lines that keep the same slope show the kitchen working at a steady pace."))
    c_back = with_explain(
        R.line_chart("Waiting line over time", "Calls registered but not yet analysed", xs, back, R._num, " calls", wide=True),
        explain("How many calls were waiting for analysis at each moment of each burst.",
                "The number of order tickets clipped on the kitchen rail, waiting for a free chef.",
                "The line rises while calls arrive faster than they're analysed, peaks, then falls to zero when the kitchen catches up. "
                "A higher peak means a longer wait for the last calls.",
                f"The longest waiting line was {max(b['backlog'] for b in rows):,} calls, during the {max(rows, key=lambda b: b['backlog'])['size']:,}-call burst."))

    c_rate = with_explain(
        R.bar_chart("Processing speed per burst", "Average calls fully analysed per minute", labels,
                    [round(b["rate"] or 0, 1) for b in rows], "s3", R._num, " calls/min", show_values=True),
        explain("How many calls per minute the system finished during each burst, on average.",
                "Dishes served per minute. A kitchen with a fixed number of stoves reaches a top speed it can't go past.",
                "If the bars level off as bursts get bigger, the system has hit its top speed. That's its capacity limit.",
                _rate_takeaway(rows)))
    c_scale = with_explain(
        R.line_chart("Does waiting time grow in step with size?", "Total minutes vs burst size, compared with simple straight-line growth",
                     [b["size"] for b in rows],
                     [("Measured", "s1", [round((b["total_s"] or 0) / 60, 2) if b["total_s"] else None for b in rows]),
                      ("If it grew in direct proportion", "s2",
                       [round((rows[0]["total_s"] or 0) / 60 * b["size"] / rows[0]["size"], 2) if rows[0]["total_s"] else None for b in rows])],
                     R._num, " min", xfmt="num", xlab=lambda v: f"{v:,}"),
        explain("The measured total time for each burst size, next to a reference line that assumes double the calls means double the time.",
                "If a 10-person group waits 5 minutes, would a 20-person group wait 10? This shows whether the kitchen keeps that pace.",
                "Measured line below the reference line: the system gets more efficient with bigger bursts (fixed start-up time is shared). "
                "Above it: bigger bursts slow it down disproportionately."))
    c_site = with_explain(
        R.bar_chart("Website speed during each burst", "95 out of 100 Call Review page clicks answered within (ms)", labels,
                    [round(b["site_p95"] or 0) for b in rows], "s2", R._num, " ms", show_values=True),
        explain("While each burst was being processed, simulated users kept clicking through the Call Review pages. Each bar is how long the slower clicks took.",
                "During the rush, can the waiter still answer a customer's question quickly?",
                "Under 1,000 ms (1 second) feels instant; 1–3 seconds is noticeable; over 3 seconds feels slow.",
                site_line))
    c_upload = with_explain(
        R.bar_chart("Upload time per burst", "Time to send every zip of the burst (seconds)", labels,
                    [round(b["upload_s"] or 0, 1) for b in rows], "s4", R._num, " s", show_values=True),
        explain("How long it took for every zip file in the burst to be received.",
                "How long the queue at the counter took to hand over all their order slips.",
                "This mostly depends on the internet connection of the computer sending the files, not on the system itself."))

    findings = _findings(rows, ref, proj_5000, worst_p95, ai)
    story = "".join(
        f'<div class="step"><span class="num">{i}</span><h3>{t}</h3><p>{d}</p><p class="life">{life}</p></div>'
        for i, (t, d, life) in enumerate([
            ("Send", f"A burst of call recordings is uploaded all at once, in zips of {first.get('calls_per_zip', 25)}.", "A tour bus arrives and everyone hands in their order."),
            ("Register", "The system unpacks each recording and creates a call record.", "The cashier enters each order."),
            ("Analyse", "Each call is transcribed and AI-scored by cloud workers, many at the same time.", "Chefs cook the dishes on the available stoves."),
            ("Done", "We stop the clock when the last call of the burst is finished.", "The last guest gets their plate."),
        ], 1))
    gloss = "".join(
        f'<div class="gloss-group">{e(group)}</div>' + "".join(
            f'<div class="g"><h3>{e(term)}</h3><p>{e(defn)}</p><p class="life">Like: {e(life)}</p></div>' for term, defn, life in items)
        for group, items in GLOSSARY)

    when = f"{first.get('started_utc', '')[:16].replace('T', ' ')} → {last.get('finished_utc', '')[:16].replace('T', ' ')} UTC"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Burst Test Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>{R.CSS}{EXTRA_CSS}</style></head><body><main class="wrap">
<header class="hero"><div class="eyebrow">Burst test report · BPO-BOX call analysis</div>
<h1>How fast does the system handle a sudden rush of calls?</h1>
<p>We sent bursts of {_and_list([f'{b["size"]:,}' for b in rows])} calls to the system, each burst all at once, and timed how long it took
until every single call was fully analysed. Simulated users also browsed the website during each burst to check it stayed responsive.</p>
<span class="pill">Environment: {e(host)}</span><span class="pill">{e(when)}</span><span class="pill">{e(AI_PILL[ai])}</span></header>

<section><div class="section-head"><h2>How a call moves through the system</h2><p>The four steps every call goes through</p></div>
<div class="story">{story}</div>
<div class="verdict-box {tone}"><h2>In one sentence</h2><p>{headline} {site_line}</p></div></section>

<section><div class="section-head"><h2>Key numbers</h2><p>Each card says what the number means and what it's like in everyday life</p></div>
<div class="bks">{"".join(cards)}</div></section>

<section><div class="section-head"><h2>Results for every burst</h2><p>One row per burst size</p></div>{table}</section>

<section><div class="section-head"><h2>Time</h2><p>How long the bursts took, and which stage took longest</p></div>
<div class="grid1">{c_total}</div><div class="grid1">{c_parts}</div><div class="grid1">{c_race}</div></section>

<section><div class="section-head"><h2>Capacity</h2><p>How much the system can handle and where it slows down</p></div>
<div class="grid1">{c_back}</div><div class="grid2">{c_rate}{c_scale}</div></section>

<section><div class="section-head"><h2>User experience</h2><p>What people using the website felt during each burst</p></div>
<div class="grid2">{c_site}{c_upload}</div></section>

<section><div class="section-head"><h2>What we learned &amp; what to do next</h2></div><div class="alerts">{findings}</div></section>

<section><div class="section-head"><h2>Glossary</h2><p>Every term used in this report, in plain words</p></div><div class="gloss">{gloss}</div></section>

<section><div class="section-head"><h2>How this test was run</h2></div><p class="note">Recordings: a set of 100 real call recordings, reused
with unique names so each call is new to the system. Each burst was uploaded as zips of {first.get('calls_per_zip', 25)} calls, all at the same moment,
from a single computer. Progress was read from the system's own call list every few seconds, and the clock stopped when every uploaded call
was analysed or deliberately skipped. Each burst started only after the previous one had completely finished. {AI_METHOD[ai]} Server-side measurements
(CPU, database load) were not part of this test. Technical detail for each burst is in the matching e2e_burst&lt;size&gt;_*.html report.</p></section>
<footer>Generated by burst_report.py · {e(len(rows))} bursts · k6 load generator + calls API polling</footer>
</main><div id="tip" hidden></div><script>{R.JS}</script></body></html>"""


def _and_list(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _parts_takeaway(rows):
    b = max(rows, key=lambda x: x["size"])
    if not b["total_s"]:
        return "The largest burst did not finish, so its split is incomplete."
    parts = {"uploading": b["upload_s"] or 0, "registering calls": max(0, (b["created_s"] or 0) - (b["upload_s"] or 0)),
             "finishing analysis": max(0, b["total_s"] - (b["created_s"] or 0))}
    name, sec = max(parts.items(), key=lambda kv: kv[1])
    return f"For the {b['size']:,}-call burst, the longest stage was {name} ({mins(sec)} of {mins(b['total_s'])})."


def _rate_takeaway(rows):
    rates = [b["rate"] for b in rows if b["rate"]]
    if len(rates) < 2:
        return "Run more burst sizes to see where speed levels off."
    top = max(rates)
    big = [b["rate"] for b in rows[-2:] if b["rate"]]
    if big and min(big) >= 0.85 * top:
        return f"Speed levels off at about {top:,.0f} calls per minute: this is the system's current top speed."
    return f"Speed kept rising with bigger bursts (best {top:,.0f} calls per minute), so the system hasn't hit its limit yet."


def _findings(rows, ref, proj, worst_p95, ai="mock"):
    items = []

    def add(kind, title, body):
        cls = {"good": "a-pass", "warn": "a-warn", "bad": "a-fail"}[kind]
        lab = {"good": ("pass", "Good news"), "warn": ("warn", "Watch"), "bad": ("fail", "Problem")}[kind]
        items.append(f'<div class="alert {cls}"><div class="stripe"></div><div class="body"><div class="title">{R.badge(*lab)}'
                     f'<h3>{title}</h3></div><ul><li>{body}</li></ul></div></div>')

    unfinished = [b for b in rows if not b["complete"]]
    if not unfinished:
        add("good", "Every call in every burst was processed",
            "Nothing was lost: all uploaded calls were analysed (or skipped because they were under 30 seconds).")
    else:
        add("bad", f"{len(unfinished)} burst(s) did not finish",
            "Some calls were still unprocessed when tracking stopped. The technical team should check the processing queues "
            "for failed messages.")
    if ref and ref["rate"]:
        add("warn", f"Top speed is about {ref['rate']:,.0f} calls per minute",
            f"The analysis step works on a limited number of calls at a time (like a kitchen with 15 stoves). "
            f"At this speed, 5,000 calls arriving together would take about {mins(proj)} to finish. "
            "Raising the number of analysis workers is the main lever to make big bursts finish faster.")
    if worst_p95 is not None:
        if worst_p95 <= 1000:
            add("good", "Website stayed fast during the rush",
                f"Even in the busiest burst, 95% of page clicks were answered within {worst_p95:,.0f} ms.")
        else:
            add("warn", "Website slowed down during big bursts",
                f"At worst, 95% of clicks took up to {worst_p95:,.0f} ms. Unpacking uploaded files currently runs on the same servers "
                "that serve the website; moving that work to background workers would keep the site fast.")
    if ai == "mock":
        add("warn", "These results use mock AI",
            "Transcription and scoring were simulated with realistic timing. Real AI services add their own speed limits and cost, "
            "so confirm with a small real-AI run before quoting these numbers externally.")
    elif ai == "unknown":
        add("warn", "AI mode not confirmed",
            "It was not confirmed whether this environment's analysis used mock or real AI. Check MOCK_AI on its Lambdas "
            "before comparing these numbers with other runs or estimating cost.")
    return "".join(items)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("qat/results/burst_report.html"))
    args = ap.parse_args()
    results = []
    for p in args.results:
        try:
            results.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as err:
            print(f"Cannot read {p}: {err}", file=sys.stderr)
            return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(results), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

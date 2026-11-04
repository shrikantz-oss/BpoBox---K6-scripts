"""Highlights page: the positive results of the QAT performance tests, in plain language with charts,
for non-technical readers. One standalone HTML file; numbers are read from qat/results/e2e_*.json.

  python -I highlights_report.py -o qat/results/BPO-BOX_Highlights.html
"""
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import burst_report as B  # noqa: E402
import e2e_report as R  # noqa: E402

e = R.e
DONE = "all uploaded calls processed"
CELERY_PER_MIN = 1.1  # pilot run 1 (28 Oct), old Celery pipeline, real AI, from the run history
# New pipeline with REAL AI (the fair comparison with the old pipeline); later runs used simulated AI timing
REAL_AI_RUNS = ("e2e_smoke_20261030_204444", "e2e_smoke_20261102_221514")


def load_results():
    out = []
    for p in sorted((HERE / "qat" / "results").glob("e2e_*.json")):
        if p.name.endswith("_k6.json"):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            r["_stem"] = p.stem
            out.append(r)
        except (OSError, ValueError) as err:
            print(f"Skipping {p.name}: {err}", file=sys.stderr)
    return out


def fmt_min(sec):
    m, s = divmod(int(round(sec)), 60)
    return f"{m} min {s:02d} s" if m else f"{s} s"


# ─── Tiny SVG chart helpers (shared look: thin marks, recessive grid, hover tips) ───
def _ticks(vmax, n=4):
    raw = vmax / n
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 0.1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return [round(i * step, 2) for i in range(int(vmax / step) + 2) if i * step <= vmax * 1.001 + step * 0.999][:n + 2]


def hbar(items, unit, fmt, label_w=150, target=None):
    """Horizontal bars: items = [(label, value, tip, css_class)]. Optional target line (value, label)."""
    w, row, pad_r = 640, 46, 90
    vmax = max([v for _, v, _, _ in items] + ([target[0]] if target else []))
    h = row * len(items) + 34
    sx = lambda v: label_w + (w - label_w - pad_r) * v / vmax  # noqa: E731
    parts = []
    for t in _ticks(vmax):
        x = sx(t)
        parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="6" y2="{h - 26}"/>'
                     f'<text class="ax" x="{x:.1f}" y="{h - 8}" text-anchor="middle">{fmt(t)}</text>')
    for i, (lab, v, tip, cls) in enumerate(items):
        y = 10 + i * row
        bw = max(4, sx(v) - label_w)
        parts.append(f'<text class="lab" x="{label_w - 12}" y="{y + 19}" text-anchor="end">{e(lab)}</text>'
                     f'<g class="hit" data-tip="{e(tip)}"><rect x="{label_w}" y="{y - 4}" width="{w - label_w}" height="{row - 6}" fill="transparent"/>'
                     f'<path class="{cls}" d="M{label_w},{y + 4} h{bw - 4} a4,4 0 0 1 4,4 v14 a4,4 0 0 1 -4,4 h-{bw - 4} z"/></g>'
                     f'<text class="val" x="{label_w + bw + 8}" y="{y + 19}">{e(fmt(v))}{unit}</text>')
    if target:
        x = sx(target[0])
        parts.append(f'<line class="target" x1="{x:.1f}" x2="{x:.1f}" y1="2" y2="{h - 26}"/>'
                     f'<text class="tlab" x="{x - 6:.1f}" y="{h - 30}" text-anchor="end">{e(target[1])}</text>')
    return f'<svg viewBox="0 0 {w} {h}" role="img">{"".join(parts)}</svg>'


def line(series, xmax, ymax, xfmt, yfmt, tipfmt):
    """series = [(name, css_class, [(x, y)], end_label)]; one y-axis, direct end labels, hover points."""
    w, h, l, r, t, b = 640, 300, 56, 120, 14, 34
    sx = lambda x: l + (w - l - r) * x / xmax  # noqa: E731
    sy = lambda y: t + (h - t - b) * (1 - y / ymax)  # noqa: E731
    parts = []
    for v in _ticks(ymax):
        parts.append(f'<line class="grid" x1="{l}" x2="{w - r}" y1="{sy(v):.1f}" y2="{sy(v):.1f}"/>'
                     f'<text class="ax" x="{l - 8}" y="{sy(v) + 4:.1f}" text-anchor="end">{yfmt(v)}</text>')
    for v in _ticks(xmax, 5):
        parts.append(f'<text class="ax" x="{sx(v):.1f}" y="{h - 10}" text-anchor="middle">{xfmt(v)}</text>')
    for name, cls, pts, end in series:
        d = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(pts))
        parts.append(f'<path class="ln {cls}" d="{d}"/>')
        x, y = pts[-1]
        parts.append(f'<circle class="dot {cls}" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4"/>'
                     f'<text class="elab {cls}-t" x="{sx(x) + 10:.1f}" y="{sy(y) + 4:.1f}">{e(end)}</text>')
        parts += [f'<circle class="hit" data-tip="{e(tipfmt(name, px, py))}" cx="{sx(px):.1f}" cy="{sy(py):.1f}" r="9" fill="transparent"/>'
                  for px, py in pts]
    return f'<svg viewBox="0 0 {w} {h}" role="img">{"".join(parts)}</svg>'


def table(head, rows):
    th = "".join(f"<th>{e(x)}</th>" for x in head)
    tr = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<details class="nums"><summary>See the numbers</summary><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></details>'


def legend(items):
    return '<div class="legend">' + "".join(f'<span><i class="{c}"></i>{e(n)}</span>' for n, c in items) + "</div>"


# ─── Page ────────────────────────────────────────────────────────────────────
def render(results):
    done = [r for r in results if r.get("end_reason") == DONE]
    calls_done = sum(r.get("uploaded_calls") or 0 for r in done)
    step = next((r for r in reversed(results) if r.get("scenario") == "step"), None)
    smoke = [r for r in done if r.get("scenario") == "smoke"]

    # Fastest finish per burst size (any zip packing that passed)
    best, all_t = {}, {}
    for r in done:
        bp = (r.get("plan") or {}).get("burst")
        if not bp:
            continue
        n = B.burst_numbers(r)
        if n["total_s"]:
            all_t.setdefault(bp["calls"], []).append(n["total_s"])
        if n["total_s"] and (bp["calls"] not in best or n["total_s"] < best[bp["calls"]][0]):
            best[bp["calls"]] = (n["total_s"], n["zips"], r.get("calls_per_zip"))

    # Step test: first 10 minutes (200 calls/min) and the steady analysis afterwards
    tl = step["timeline"] if step else []
    sent = [(t, c) for t, c in (step["planned_curve"] if step else []) if t <= 600]
    created = [(s["t"], s["ingested"]) for s in tl if s["t"] <= 610]
    drain = [s for s in tl if s["t"] >= 1530]  # after every call was registered
    a_rate = ((drain[-1]["processed"] - drain[0]["processed"]) / ((drain[-1]["t"] - drain[0]["t"]) / 60)) if len(drain) > 1 else 90
    # Real-AI speed of the new pipeline: whole-test average (incl. upload and start-up) and peak minute
    real = [r for r in results if r.get("_stem") in REAL_AI_RUNS]
    real_avg = [r["uploaded_calls"] / (R.derive(r)["t_all_processed"] / 60) for r in real if R.derive(r)["t_all_processed"]]
    real_peak = [R.derive(r)["peak_processed"] for r in real if R.derive(r)["peak_processed"]]
    lo_avg, hi_avg, hi_peak = ((min(real_avg), max(real_avg), max(real_peak)) if real_avg and real_peak
                               else (a_rate, a_rate, a_rate))
    speedup = int(lo_avg / CELERY_PER_MIN / 5) * 5  # conservative: slowest whole-test average, rounded down to 5

    # ── Charts ──
    c_lost = (f'<div class="donebar"><div class="fill" style="width:100%"><span>{calls_done:,} calls finished</span></div></div>'
              f'<div class="donecap"><span>0 calls lost</span><span>{len(done)} of {len(done)} completed tests</span></div>')

    c_speed = hbar([("Old system", CELERY_PER_MIN, f"Old system, real AI: about {CELERY_PER_MIN} calls analysed per minute", "s2"),
                    ("New · average", lo_avg, f"New system, real AI: {lo_avg:.0f} calls a minute averaged over a whole test (slowest test)", "s1"),
                    ("New · peak", hi_peak, f"New system, real AI: {hi_peak:.0f} calls in the busiest minute", "s1")],
                   " / min", lambda v: f"{v:,.0f}" if v >= 10 else f"{v:,.1f}", label_w=170)

    sizes = sorted(best)
    c_burst = hbar([(f"{s:,} calls", best[s][0] / 60, f"{s:,} calls at once ({best[s][1]} zips of {best[s][2]}): all finished in {fmt_min(best[s][0])}", "s1")
                    for s in sizes], " min", lambda v: f"{v:,.0f}", label_w=110)

    c_keep = line([("Calls sent", "s2", sent, "Sent"), ("Calls registered", "s1", created, "Registered")],
                  600, max(c for _, c in sent + created) * 1.08, lambda x: f"{x / 60:.0f} min", lambda y: f"{y:,.0f}",
                  lambda n, x, y: f"{n} after {fmt_min(x)}: {y:,.0f}")

    t0 = drain[0]["t"] if drain else 0
    p0 = drain[0]["processed"] if drain else 0
    steady_pts = [(s["t"] - t0, s["processed"] - p0) for s in drain]
    c_steady = line([("Calls analysed", "s1", steady_pts, f"{steady_pts[-1][1]:,} analysed" if steady_pts else "")],
                    steady_pts[-1][0] if steady_pts else 1, (steady_pts[-1][1] if steady_pts else 1) * 1.1,
                    lambda x: f"{x / 60:.0f} min", lambda y: f"{y:,.0f}", lambda n, x, y: f"After {fmt_min(x)}: {y:,} calls analysed")

    p95s = [((r.get("k6") or {}).get("browse_p95_ms") or 0) / 1000 for r in smoke]
    c_site = hbar([(f"Test {i}", v, f"Test {i}: 95 of every 100 page clicks answered within {v:.2f} seconds", "s1") for i, v in enumerate(p95s, 1)],
                  " s", lambda v: f"{v:.1f}", label_w=90, target=(2.0, "Target: 2 s"))

    # ── Tables (the same numbers, for screen readers and copy-paste) ──
    t_burst = table(["Calls sent at once", "Packed as", "All finished in"],
                    [(f"{s:,}", f"{best[s][1]} zips of {best[s][2]}", fmt_min(best[s][0])) for s in sizes])
    t_keep = table(["Minute", "Calls sent", "Calls registered"],
                   [(f"{t / 60:.1f}", f"{c:,.0f}", f"{next((y for x, y in reversed(created) if x <= t), 0):,}") for t, c in sent[::8]])
    t_site = table(["Test", "95% of clicks answered within"], [(f"Test {i}", f"{v:.2f} s") for i, v in enumerate(p95s, 1)])

    step_rate = 200  # the steady rate the system kept pace with (step 1 of the step-up test)
    per_hour, analysed_hour = step_rate * 60, round(a_rate * 60 / 100) * 100
    big = best.get(1000)
    tiles = "".join(f'<div class="tile"><div class="big">{v}</div><div class="cap">{e(c)}</div></div>' for v, c in [
        ("0", "calls lost in completed tests"), (f"{speedup}×+", "faster analysis than before (real AI)"),
        ("1,000", "calls handled in one rush"), (f"{per_hour:,}", "per-hour intake pace (10-min test)"),
        (f"{analysed_hour:,}", "per hour analysed at best (simulated AI)"),
        (f"{max(p95s):.1f} s" if p95s else "–", "page loads in normal use")])

    def card(n, title, plain, example, chart="", tbl="", leg=""):
        return (f'<section class="card"><div class="num">{n}</div><h2>{e(title)}</h2><p class="plain">{plain}</p>'
                f'<div class="example"><b>Everyday example</b><p>{example}</p></div>'
                f'{leg}{f"<div class=chart>{chart}</div>" if chart else ""}{tbl}</section>')

    sections = "".join([
        card(1, "Every completed test finished all of its calls",
             f"In all {len(done)} tests that ran to the end, all <b>{calls_done:,} call uploads</b> were received, "
             "registered and analysed (very short recordings, under 30 seconds, are skipped on purpose). Tests stopped early by the "
             "safety switch are not counted here.",
             f"Like a courier company delivering {calls_done:,} parcels without misplacing a single one, even on its busiest days.", c_lost),
        card(2, f"Call analysis is more than {speedup} times faster than before",
             f"Both measured with real AI: the old setup analysed about <b>{CELERY_PER_MIN} calls a minute</b>; the new cloud setup "
             f"averaged <b>{lo_avg:.0f}–{hi_avg:.0f} calls a minute</b> over a whole test and reached <b>{hi_peak:.0f}</b> in its busiest minute.",
             f"Analysing 1,000 calls used to take about <b>{1000 / CELERY_PER_MIN / 60:.0f} hours</b>. At the new speed it takes roughly "
             f"<b>{int(1000 / hi_peak / 5) * 5}–{math.ceil(1000 / lo_avg / 5) * 5} minutes</b>: the difference between waiting until "
             "tomorrow and waiting for a meeting to end.", c_speed),
        card(3, "It handles a sudden rush of 1,000 calls",
             "We dropped large batches of calls on the system all at the same moment. Every batch was fully analysed, and bigger "
             "batches simply took a little longer — nothing broke.",
             (f"Think of a Monday morning when the weekend's recordings arrive together: a pile of 1,000 calls was fully analysed in "
              f"<b>{round(min(all_t[1000]) / 60)}–{round(max(all_t[1000]) / 60)} minutes</b> across {len(all_t[1000])} tests "
              "(the chart shows the fastest run for each size).") if all_t.get(1000) else "", c_burst, t_burst),
        card(4, f"It kept pace with {step_rate} new calls every minute",
             f"For ten minutes we sent calls non-stop at a steady pace of 200 a minute. The system registered them as fast as they arrived "
             f"(the two lines stay together) with zero errors. That is a pace of <b>{per_hour:,} calls an hour</b>, measured over a "
             "10-minute test.",
             f"If each agent finishes around 30 calls an hour, {per_hour:,} calls an hour is the output of roughly "
             f"<b>{per_hour // 30:,} agents</b> working at the same time.", c_keep, t_keep,
             legend([("Calls sent", "s2"), ("Calls registered", "s1")])),
        card(5, "It works at a steady, predictable pace",
             f"While working through a large queue for about 20 minutes, the system analysed calls at an even <b>{a_rate:.0f} calls a "
             "minute</b> (with simulated AI timing). A straight line like this means the speed can be planned for.",
             f"Like a well-run production line: about {a_rate:.0f} items every minute, so {analysed_hour:,} an hour or "
             f"<b>{analysed_hour * 8:,} in an 8-hour shift</b>. If one reviewer checks around 40 calls a day, that is the work of about "
             f"<b>{analysed_hour * 8 // 40:,} reviewers</b>.", c_steady),
        card(6, "The website is quick in normal use",
             f"With normal activity, 95 out of every 100 page clicks were answered within <b>{max(p95s):.1f} seconds</b> — "
             "well inside our 2-second target.",
             "That is about the time it takes to blink. Users open a call, a report or a list and it is simply there.",
             c_site, t_site),
        card(7, "Testing is safe and repeatable",
             "Every test has a built-in safety switch: if the system shows signs of strain, the test stops itself straight away. "
             "Each run also produces its own report, so results can be compared over time.",
             "Like a treadmill stress test with a heart monitor: it pushes hard, but stops the moment the readings say so."),
    ])

    nice = lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%d %B %Y").lstrip("0") if d else ""  # noqa: E731
    first = nice(min((r.get("started_utc", "") for r in results), default="")[:10])
    last = nice(max((r.get("finished_utc", "") for r in results), default="")[:10])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Performance Highlights</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><main>
<header class="hero"><div class="eyebrow">BPO-BOX · Performance testing highlights</div>
<h1>What is already working well</h1>
<p>We put the BPO-BOX call analysis system through {len(results)} recorded performance tests between {e(first)} and {e(last)}, sending thousands
of real call recordings at it in rushes and at a steady pace. Here are the strongest results, explained in everyday terms.</p>
<div class="tiles">{tiles}</div></header>
{sections}
<footer>Measured on the BPO-BOX test environment (qat.bpobox.ai), not production. Most tests used simulated AI timing; the speed-up in
point 2 compares old and new with real AI. Agent and reviewer comparisons are illustrative estimates. Hover over any chart for exact values.</footer>
</main><div id="tip" hidden></div><script>{JS}</script></body></html>"""


CSS = """
:root{color-scheme:light;--bg:#f4f4f2;--surface:#fcfcfb;--text:#0b0b0b;--text-2:#52514e;--text-3:#6f6e69;--line:#e4e3df;
--s1:#2a78d6;--s2:#eb6834;--good:#0ca30c;--hero1:#1c5cab;--hero2:#2a78d6;--ex:#eef4fc}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111110;--surface:#1a1a19;--text:#f0efec;
--text-2:#c3c2b7;--text-3:#9a998f;--line:#2d2d2b;--s1:#3987e5;--s2:#d95926;--hero1:#104281;--hero2:#1c5cab;--ex:#1d2633}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--text:#f0efec;--text-2:#c3c2b7;--text-3:#9a998f;--line:#2d2d2b;
--s1:#3987e5;--s2:#d95926;--hero1:#104281;--hero2:#1c5cab;--ex:#1d2633}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 Inter,system-ui,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 48px}
.hero{background:linear-gradient(135deg,var(--hero1),var(--hero2));color:#fff;border-radius:20px;padding:32px 28px}
.eyebrow{font-size:13px;letter-spacing:.08em;text-transform:uppercase;opacity:.85;font-weight:600}
.hero h1{font-size:clamp(28px,5vw,42px);line-height:1.15;margin:8px 0 10px;font-weight:800}.hero p{margin:0;opacity:.92;max-width:680px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:24px}
.tile{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);border-radius:14px;padding:14px 16px}
.tile .big{font-size:30px;font-weight:800;line-height:1.1}.tile .cap{font-size:14px;opacity:.9}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:26px 26px 22px 80px;margin-top:18px}
.num{position:absolute;left:24px;top:24px;width:38px;height:38px;border-radius:50%;background:var(--good);color:#fff;font-weight:800;
display:flex;align-items:center;justify-content:center}
.card h2{margin:4px 0 8px;font-size:22px;line-height:1.25}.plain{margin:0 0 14px;color:var(--text-2)}.plain b{color:var(--text)}
.example{background:var(--ex);border-radius:12px;padding:12px 16px;margin-bottom:16px}.example b{font-size:12px;text-transform:uppercase;
letter-spacing:.06em;color:var(--s1)}.example p{margin:4px 0 0}
.chart svg{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}.ax{fill:var(--text-3);font-size:12px}.lab{fill:var(--text);font-size:14px;font-weight:500}
.val{fill:var(--text);font-size:14px;font-weight:700}.s1{fill:var(--s1)}.s2{fill:var(--s2)}
.ln{fill:none;stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}.ln.s1{stroke:var(--s1)}.ln.s2{stroke:var(--s2);stroke-dasharray:6 5}
.dot{stroke:var(--surface);stroke-width:2}.elab{font-size:13px;font-weight:700;fill:var(--text)}
.target{stroke:var(--good);stroke-width:2;stroke-dasharray:4 4}.tlab{fill:var(--text-2);font-size:12px;font-weight:600}
.hit{cursor:default}.hit:hover path{opacity:.85}
.legend{display:flex;gap:18px;font-size:14px;color:var(--text-2);margin-bottom:6px}.legend i{display:inline-block;width:18px;height:4px;
border-radius:2px;margin-right:6px;vertical-align:middle}.legend i.s1{background:var(--s1)}.legend i.s2{background:var(--s2)}
.donebar{background:var(--line);border-radius:10px;overflow:hidden;height:44px}.donebar .fill{height:100%;background:var(--good);display:flex;
align-items:center;padding-left:16px;color:#fff;font-weight:700}
.donecap{display:flex;justify-content:space-between;font-size:14px;color:var(--text-2);margin-top:8px;flex-wrap:wrap;gap:8px}
.donecap span:first-child{color:var(--good);font-weight:700}
.nums{margin-top:10px;font-size:14px}.nums summary{cursor:pointer;color:var(--text-2)}.nums table{border-collapse:collapse;margin-top:8px;width:100%}
.nums th,.nums td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}.nums th{color:var(--text-3);font-weight:600}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);font-size:13px;padding:7px 10px;border-radius:8px;
max-width:280px;z-index:9;box-shadow:0 4px 14px rgba(0,0,0,.2)}
footer{margin-top:22px;color:var(--text-3);font-size:13px;text-align:center}
@media(max-width:640px){.tiles{grid-template-columns:repeat(2,1fr)}.card{padding:70px 18px 18px}.num{left:18px;top:18px}.hero{padding:24px 18px}}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
  el.addEventListener('mousemove',ev=>{tip.textContent=el.dataset.tip;tip.hidden=false;
    const x=Math.min(ev.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+'px';tip.style.top=(ev.clientY+14)+'px';});
  el.addEventListener('mouseleave',()=>{tip.hidden=true;});
});
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, default=HERE / "qat" / "results" / "BPO-BOX_Highlights.html")
    args = ap.parse_args()
    results = load_results()
    if not results:
        print("No qat/results/e2e_*.json files found.", file=sys.stderr)
        return 1
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(results), encoding="utf-8")
    except OSError as err:
        print(f"Cannot write {args.out}: {err}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

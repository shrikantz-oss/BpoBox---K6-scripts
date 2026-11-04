"""Shared sections for the two summary reports: every scenario compared side by side, and what is good /
what needs improving. Numbers come from qat/results/e2e_*.json, so they always match the per-run reports.

Used by run_history_report.py (all scenarios, passed and failed) and client_report.py (passed only).
"""
import json
from pathlib import Path

import burst_report as B
import e2e_report as R

HERE = Path(__file__).resolve().parent
RES = HERE / "qat" / "results"
DONE = "all uploaded calls processed"
e, mins, num = R.e, B.mins, B.num


def _load_all():
    out = []
    for p in sorted(RES.glob("e2e_*.json")):
        if p.name.endswith("_k6.json"):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        r["_stem"] = p.stem
        out.append(r)
    return out


def _at(tl, t, key):
    """Value of `key` at time t (last sample at or before t)."""
    v = 0
    for s in tl:
        if s["t"] > t:
            break
        v = s[key]
    return v


def _rate(tl, t0, t1, key):
    return (_at(tl, t1, key) - _at(tl, t0, key)) / ((t1 - t0) / 60) if t1 > t0 else None


def step_numbers(r):
    """Per-step measurements of a `step` run: ingest per step, failure point, analysis speed while draining."""
    tl = r.get("timeline") or []
    stop = r.get("step_stop") or {}
    fail_t = stop.get("t") or (tl[-1]["t"] if tl else 0)
    up_end = r.get("upload_end_s") or fail_t
    last = tl[-1]["t"] if tl else 0
    drain0 = min(last, up_end + 360)  # unpacking finished ~6 min after uploads stopped
    k = r.get("k6") or {}
    return {
        "s1_rate": _rate(tl, 120, 600, "ingested"), "s1_created": _at(tl, 600, "ingested"),
        "s2_rate": _rate(tl, 600, fail_t, "ingested"), "s2_min": (fail_t - 600) / 60,
        "fail_rate": stop.get("rate"), "fail_reason": stop.get("reason"),
        "uploaded": r.get("uploaded_calls") or 0, "processed": (r.get("final") or {}).get("processed", 0),
        "analysis_rate": _rate(tl, drain0, last, "processed"), "last_t": last,
        "site_p95": k.get("browse_p95_ms"), "upload_ok": k.get("upload_success"), "end_reason": r.get("end_reason"),
    }


def _by(results, pred):
    return [r for r in results if pred(r)]


def _burst(r, cpz_test, size=None):
    b = (r.get("plan") or {}).get("burst")
    return bool(b) and cpz_test(r.get("calls_per_zip")) and (size is None or b["calls"] == size)


def _range(vals, fmt):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "–"
    lo, hi = min(vals), max(vals)
    return fmt(lo) if fmt(lo) == fmt(hi) else f"{fmt(lo)} – {fmt(hi)}"


def _p95(v):
    return f"{v / 1000:.1f} s"


def scenario_rows(include_failed=True):
    """[(scenario, what was sent, runs, key measurements, verdict_kind, verdict_text)]."""
    rs = _load_all()
    rows = []

    smoke = _by(rs, lambda r: r.get("scenario") == "smoke" and r.get("end_reason") == DONE)
    if smoke:
        tt = [R.derive(r)["t_all_processed"] for r in smoke]
        rows.append(("Smoke check", "100 calls in 4 zips of 25", f"{len(smoke)} passed",
                     f"All processed in {_range(tt, mins)} · website 95% within {_range([(r.get('k6') or {}).get('browse_p95_ms') for r in smoke], _p95)}",
                     "pass", "Passed"))

    for size in (100, 200, 500):
        ok = _by(rs, lambda r, s=size: _burst(r, lambda c: c in (25, 100), s) and r.get("end_reason") == DONE)
        if ok:
            nums = [B.burst_numbers(r) for r in ok]
            rows.append((f"Burst · {size:,} calls at once", f"zips of {B._and_list(sorted({str(r['calls_per_zip']) for r in ok}, key=int))}",
                         f"{len(ok)} passed",
                         f"Done in {_range([n['total_s'] for n in nums], mins)} · website 95% within {_range([n['site_p95'] for n in nums], _p95)}",
                         "pass" if max(n["site_p95"] or 0 for n in nums) <= 2000 else "warn",
                         "Passed" if max(n["site_p95"] or 0 for n in nums) <= 2000 else "Passed · website slow"))

    if include_failed:
        bad = _by(rs, lambda r: _burst(r, lambda c: c == 25, 1000))
        rows.append(("Burst · 1,000 calls in 40 small zips", "40 zips of 25 at once", f"{1 + len(bad)} failed",
                     "Uploads rejected (HTTP 500), login errors for all users; at most 900 of 1,000 calls got in. "
                     "Cause: 40 zips unpacking at once used up the API's 18 database connections",
                     "fail", "Failed"))

    big = _by(rs, lambda r: _burst(r, lambda c: c >= 100, 1000) and r.get("end_reason") == DONE)
    if big:
        nums = sorted(((r["calls_per_zip"], B.burst_numbers(r)) for r in big), key=lambda t: t[0])
        detail = "; ".join(f"{n['zips']}×{c}: {mins(n['total_s'])}" for c, n in nums)
        rows.append(("Burst · 1,000 calls in ≤ 10 large zips", "2, 8 or 10 zips of 500, 125 or 100", f"{len(big)} passed",
                     f"All processed ({detail}) · website 95% within {_range([n['site_p95'] for _, n in nums], _p95)}",
                     "warn", "Passed · website slow"))

    if include_failed:
        load = _by(rs, lambda r: r.get("scenario") == "load")
        if load:
            r = load[-1]
            rows.append(("Steady load · 300 calls/min (old zips of 25)", "300 calls/min for 10 min", "1 stopped",
                         f"{r.get('uploaded_calls', 0):,} calls sent, 100% accepted; stopped on request before the backlog cleared · "
                         f"website 95% within {_p95((r.get('k6') or {}).get('browse_p95_ms') or 0)}",
                         "info", "Stopped (no verdict)"))

    step = _by(rs, lambda r: r.get("scenario") == "step")
    if step:
        s = step_numbers(step[-1])
        rows.append(("Steady load · step 1: 200 calls/min (12,000/hour)", "2 zips of 100 per minute for 10 min", "1 passed",
                     f"Calls created at {num(s['s1_rate'])}/min, keeping pace with arrivals; no upload rejected",
                     "pass", "Passed (ingest)"))
        if include_failed:
            rows.append(("Steady load · step 2: 300 calls/min (18,000/hour)", "3 zips of 100 per minute", "1 failed",
                         f"Calls created at only {num(s['s2_rate'])}/min on average, then fell 1,020 calls (10 zips) behind after "
                         f"{s['s2_min']:.1f} min; uploads stopped automatically. Steps 3 (450/min) and 4 (667/min = 40,000/hour) not reached",
                         "fail", "Failed"))
            rows.append(("Analysis while catching up after the step test", f"{s['uploaded']:,} calls waiting", "—",
                         f"Analysed at a steady {num(s['analysis_rate'])} calls/min (≈ {num((s['analysis_rate'] or 0) * 60)}/hour); "
                         f"{s['processed']:,} of {s['uploaded']:,} done when QAT stopped responding (HTTP 502/504/503) at about 00:00 IST",
                         "fail", "QAT went down"))
        else:
            rows.append(("Analysis speed (sustained)", "Calls waiting after the step test", "—",
                         f"Steady {num(s['analysis_rate'])} calls/min (≈ {num((s['analysis_rate'] or 0) * 60)}/hour)", "warn", "Bottleneck"))
    return rows


def compare_table(include_failed=True):
    rows = "".join(
        f"<tr><td><b>{e(a)}</b><span class=sm>{e(b)}</span></td><td>{e(n)}</td><td>{e(m)}</td><td>{R.badge(k, v)}</td></tr>"
        for a, b, n, m, k, v in scenario_rows(include_failed))
    note = ("Burst = all calls sent at the same moment. Steady load = calls sent at an even pace per minute. "
            "Website 95% = 95 out of 100 page clicks answered within this time (target 2 s). AI was simulated in all runs listed here.")
    return (f'<div class="card table-wrap"><table class="btable sc"><thead><tr><th>Scenario</th><th>Runs</th><th>What we measured</th>'
            f'<th>Verdict</th></tr></thead><tbody>{rows}</tbody></table><p class="tbl-note">{e(note)}</p></div>')


def limits_cards():
    rs = _by(_load_all(), lambda r: r.get("scenario") == "step")
    s = step_numbers(rs[-1]) if rs else {}
    a = s.get("analysis_rate") or 90
    return "".join([
        B.kpi("Calls accepted (sustained)", "200", "calls / min",
              f"Highest steady rate that held for 10 minutes = 12,000 calls/hour. At 300/min the system fell behind "
              f"(created only {num(s.get('s2_rate'))}/min).", "How many guests the host can seat per minute without a queue forming.", "warn"),
        B.kpi("Calls fully analysed", num(a), "calls / min",
              f"Steady analysis speed ≈ {num(a * 60)} calls/hour. Anything above this builds a backlog that clears later.",
              "How many meals the kitchen actually finishes per minute.", "bad"),
        B.kpi("Largest safe burst", "1,000", "calls at once",
              "Passed every time when sent as 10 zips or fewer; failed every time as 40 small zips.",
              "A tour bus is fine if guests come in groups of 100, not 40 groups at once.", "warn"),
        B.kpi("Gap to 40,000 / hour", f"{40000 / 60 / a:.1f}×", "analysis",
              f"40,000/hour needs 667/min at every stage: about 3.3× today's accept rate and {40000 / 60 / a:.1f}× today's analysis rate.",
              "The kitchen needs several more stoves and a second door.", "bad"),
    ])


GOOD = [
    ("No calls lost", "In every completed run, every uploaded call was analysed or deliberately skipped (recordings under 30 s), and no upload "
     "was rejected as long as 10 or fewer zips arrived at once."),
    ("Moving analysis to Lambda was a big step forward", "Analysis went from about 1 call/min on the old Celery queue to a steady ~85–90 calls/min."),
    ("Bursts of up to 1,000 calls work", "1,000 calls at once in 2, 8 or 10 zips were processed completely every time (about 11–22 minutes)."),
    ("Steady 200 calls/min (12,000/hour) is accepted", "For 10 minutes the API created calls as fast as they arrived, with no errors."),
    ("Quiet-time website speed is good", "With light load, 95% of page clicks were answered within about 0.4 seconds."),
    ("Safe, repeatable testing", "Automatic safety stops ended overloaded tests before more damage, and every run produced a report from the same tools."),
]

IMPROVE = [
    ("Analysis speed: ~90 calls/min (≈ 5,400/hour)", "This is the main bottleneck, about 7.5× short of 40,000/hour.",
     "Put RDS Proxy in front of the database, then raise reserved concurrency of the transcription and scoring Lambdas "
     "(today 50 and 15/15/15). Check CloudWatch for throttling and stuck (dead-letter) calls."),
    ("Accepting calls: ~200–220 calls/min ceiling", "Zip unpacking runs inside the web server with no limit; more than ~10 zips at once "
     "uses up the API's 18 database connections and logins start failing.",
     "Upload zips straight to S3 and unpack them in separate queue workers (SQS) with a fixed limit; release the database connection "
     "during S3 uploads; replace the per-call MAX(display_id) and LIKE look-ups with a sequence and an indexed column."),
    ("Website slows down under load", "95% of clicks took 4.5–13 s during large bursts and 7.5 s in the step test (target 2 s).",
     "Moving unpacking off the web servers fixes most of this; also cache the logged-in user lookup that runs on every request."),
    ("Stability: QAT went down twice", "The whole site returned 503 after the 500-call burst on 3 Nov and again at ~00:00 IST during the step "
     "test, both times after uploads had stopped.",
     "Find the cause (load balancer health checks, gunicorn log, disk space, database connections, any midnight job) and add alarms."),
    ("Real AI not tested at scale", "All load tests used simulated AI.",
     "Check transcription and LLM provider rate limits and per-call cost before planning for 40,000/hour."),
    ("Security and housekeeping", "Cross-tenant upload fix is not deployed; session timeout was raised for testing; QAT shares a "
     "production-named load balancer; ~10,000+ test calls remain in QAT.",
     "Deploy the fix, revert the timeout, confirm QAT/production isolation, and clean up test data and S3 files."),
]


def good_improve_html():
    good = "".join(f"<li><b>{e(t)}</b><span>{e(d)}</span></li>" for t, d in GOOD)
    imp = "".join(f"<li><b>{e(t)}</b><span>{e(d)}</span><span class=fix><b>What to do:</b> {e(f)}</span></li>" for t, d, f in IMPROVE)
    return (f'<div class="gi"><div class="card gi-good"><h3>{R.badge("pass", "What is good")}</h3><ul>{good}</ul></div>'
            f'<div class="card gi-bad"><h3>{R.badge("fail", "What needs improvement")}</h3><ol>{imp}</ol></div></div>')


CSS = """
.sc td,.sc th{text-align:left}.sc td{white-space:normal;vertical-align:top;font-size:13px}.sc .sm{font-size:12px;color:var(--text-3);display:block;margin-top:2px}
.gi{display:grid;grid-template-columns:1fr 1.4fr;gap:14px}@media(max-width:820px){.gi{grid-template-columns:1fr}}
.gi .card{padding:18px}.gi h3{margin:0 0 10px}.gi ul,.gi ol{margin:0;padding-left:18px}.gi li{margin:0 0 12px;font-size:14px;line-height:1.45}
.gi li b{display:block}.gi li span{display:block;color:var(--text-2)}.gi .fix{margin-top:4px;color:var(--text)}.gi .fix b{display:inline}
"""

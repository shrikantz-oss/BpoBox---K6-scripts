"""Run history report: every test run attempted on QAT (passed, failed, stopped, aborted), each error,
its root cause, what was done about it, and the backend/infra issues still open. One standalone HTML page.

  python -I run_history_report.py -o qat/results/BPO-BOX_Test_Run_History.html

The narrative (RUNS / ERRORS / TOOLING / BACKEND) is curated from the test session; numbers for runs that
wrote a qat/results/e2e_*.json are read from that file so they always match the per-run reports.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import burst_report as B  # noqa: E402
import e2e_report as R  # noqa: E402
import scenario_compare as S  # noqa: E402

e, mins = R.e, B.mins

# status: pass | fail (system failed) | stop (stopped by us/user or tooling, not a system verdict) | abort (never really ran)
# result: qat/results/e2e_<...>.json stem, when the run wrote one
RUNS = [
    # (n, IST, test, pipeline, status, result_stem, outcome, problem, root cause, action)
    (1, "28 Oct 17:03", "Pilot · 100 calls, 1 zip", "Celery, real AI", "stop", None,
     "100 registered, 24 analysed after 21 min, stopped",
     "Analysis ~1.1 calls/min; script waited for 8 short calls that are never analysed",
     "Celery queue too slow; finish check ignored deliberately skipped calls (<30 s)",
     "Finish check counts skipped calls; added track-only mode. Pipeline later moved to Lambda."),
    (2, "28 Oct 17:24", "Re-check of pilot (track only)", "Celery, real AI", "stop", None,
     "24 analysed, 8 skipped, 68 waiting", "Script syntax error during an edit", "Tooling bug", "Fixed."),
    (3, "29 Oct 19:05", "Pilot · 100 calls", "Lambda, real AI", "fail", None,
     "88 of 92 analysed in ~2.5 min; 4 calls stuck",
     "35.6% website errors: 'certificate signed by unknown authority', connect timeouts",
     "Network on the test PC (likely antivirus/VPN TLS inspection, unconfirmed); 4 calls stuck in the pipeline",
     "Network issue did not recur. 4 stuck calls: open (check DLQs)."),
    (4, "29 Oct 19:25", "Pilot · 100 calls (new file names)", "Lambda, real AI", "stop", None,
     "82 of 92 analysed in ~4 min; 0% errors; website p95 0.4 s",
     "5–10% of calls finish late on retry", "Lambda retries (timeout / rate limit, unconfirmed)",
     "Open: check Lambda timeouts, DLQs and rate limits."),
    (5, "30 Oct 20:45", "Smoke · 100 calls (4 zips)", "Lambda, real AI", "pass", "e2e_smoke_20261030_204444",
     "Passed with a watch point (speed 44/min)", "—", "—", "—"),
    (6, "2 Nov 22:07", "Smoke · 100 calls", "Lambda, real AI", "stop", "e2e_smoke_20261102_220703",
     "Stopped during upload (75 sent)", "Run ended when the Claude session was interrupted", "Runs were tied to the Claude terminal",
     "Restart waits for leftover calls first; later runs moved to a separate window."),
    (7, "2 Nov 22:12", "Smoke restart", "—", "abort", None, "Did not start", "Blank OTP rejected",
     "OTP typed before the prompt appeared", "Wait for the prompt before typing."),
    (8, "2 Nov 22:13", "Smoke restart", "—", "abort", None, "Did not start",
     "HTTP 429 'Please wait 79 second(s)…'", "QAT allows one OTP request per minute", "Retry waits 85 s."),
    (9, "2 Nov 22:15", "Smoke · 100 calls", "Lambda, real AI", "pass", "e2e_smoke_20261102_221514",
     "Passed 8/8 in 3:11, website p95 0.39 s", "—", "—", "—"),
    (10, "3 Nov 00:50", "Smoke · 100 calls", "Lambda, simulated AI", "pass", "e2e_smoke_20261103_005044",
     "Passed in 2:39", "—", "—", "—"),
    (11, "3 Nov 01:20", "Load · 300 calls/min", "Lambda, simulated AI", "stop", "e2e_load_20261103_012030",
     "Stopped by request after 10 min: 2,700 calls sent, 100% accepted, analysis ~100/min, 1,793 waiting, website p95 2.7 s",
     "Watcher raised a false 'runner exited' alarm", "Watcher quoting bug", "Watcher now tracks the process ID."),
    (12, "3 Nov 01:34", "Load rerun", "Lambda, simulated AI", "stop", None,
     "Waited ~24 min for 1,490 leftover calls, then stopped when focus moved to burst tests",
     "Leftover calls from session ingestion would have skewed results", "Shared test group",
     "Runs now wait until the group has no waiting calls."),
    (13, "3 Nov 02:08", "Burst · 100 calls (4×25)", "Lambda, simulated AI", "pass", "e2e_burst100_20261103_020819",
     "All processed in 1:52", "Test kept running ~5 min after finishing", "Fixed 5-min browse window",
     "Test now stops itself the moment every call is processed."),
    (14, "3 Nov 02:14", "Burst · 200 calls", "—", "stop", None, "Stopped by request just after upload started",
     "Same fixed browse window", "Tooling", "Fixed (see run 13)."),
    (15, "3 Nov 02:15", "Burst · 100 calls (4×25)", "Lambda, simulated AI", "pass", "e2e_burst100_20261103_021528",
     "All processed in 4:34; sequence then stopped", "A single Ctrl+C in the window ended the sequence",
     "Ctrl+C (e.g. when copying text) treated as stop", "Stopping between bursts now needs a second Ctrl+C."),
    (16, "3 Nov 02:27", "Burst · 100 calls (4×25)", "Lambda, simulated AI", "pass", "e2e_burst100_20261103_022718",
     "All processed in 2:35", "—", "—", "—"),
    (17, "3 Nov 02:31", "Burst · 200 calls (8×25)", "Lambda, simulated AI", "pass", "e2e_burst200_20261103_023104",
     "All processed in 4:41", "—", "—", "—"),
    (18, "3 Nov 02:36", "Burst · 500 calls (20×25)", "Lambda, simulated AI", "stop", None,
     "500 uploaded, then tracking lost", "Claude app crashed and closed the terminal running the test",
     "Runs were tied to the Claude terminal", "Runs now start in a separate PowerShell window with a log file."),
    (19, "3 Nov 02:44", "Burst · 500 calls (resume)", "Lambda, simulated AI", "fail", None,
     "Stopped at 03:01; QAT (and DEV) then returned HTTP 503",
     "HTTP 504 on the calls list, then 503 for the whole site",
     "API server overloaded (disk / memory / worker hang — unconfirmed)",
     "QAT recovered by 14:11. Added safety stop: 4 failed checks in a row ends the test."),
    (20, "3 Nov 14:16", "Burst · 100 calls", "Lambda, simulated AI", "stop", None,
     "Window closed after ~2 min (42 of 100 done)", "—", "Window closed", "Restarted."),
    (21, "3 Nov 14:43", "Burst · 100 calls (4×25)", "Lambda, simulated AI", "pass", "e2e_burst100_20261103_144146", "", "—", "—", "—"),
    (22, "3 Nov 14:49", "Burst · 200 calls (8×25)", "Lambda, simulated AI", "pass", "e2e_burst200_20261103_144802", "", "—", "—", "—"),
    (23, "3 Nov 14:55", "Burst · 500 calls (20×25)", "Lambda, simulated AI", "pass", "e2e_burst500_20261103_145406", "",
     "Website p95 4.5 s", "Zip unpacking shares the web servers", "Open (backend fix 1)."),
    (24, "3 Nov 15:06", "Burst · 1,000 calls (40×25)", "Lambda, simulated AI", "fail", None,
     "Database stopped answering; stopped by hand",
     "Uploads HTTP 500 'Authentication service temporarily unavailable'; token refresh 500; list 401; read timeouts; tracker crashed so the safety stop did not fire",
     "API database connection pool (18) used up by 40 zips unpacking at once",
     "Tracker now counts every network error as a failed check. Pool: see backend fix 2."),
    (25, "3 Nov 16:19", "Burst · 100 calls (4×25)", "Lambda, simulated AI", "pass", "e2e_burst100_20261103_161954", "", "—", "—", "—"),
    (26, "3 Nov 16:24", "Burst · 200 calls (8×25)", "Lambda, simulated AI", "pass", "e2e_burst200_20261103_162400", "", "—", "—", "—"),
    (27, "3 Nov 16:30", "Burst · 500 calls (20×25)", "Lambda, simulated AI", "pass", "e2e_burst500_20261103_163042", "",
     "Website p95 4.6 s", "Zip unpacking shares the web servers", "Open (backend fix 1)."),
    (28, "3 Nov 16:44", "Burst · 1,000 calls (40×25)", "Lambda, simulated AI", "fail", "e2e_burst1000_20261103_164254",
     "Safety stop ended the test; 750 of 1,000 calls got in",
     "10 of 40 uploads HTTP 500; read timeouts; database health check timed out",
     "Same connection-pool exhaustion", "2,000-call burst skipped. Backend fix 2 open."),
    (29, "3 Nov 18:18", "Burst · 1,000 calls (40×25), after DB config change", "Lambda, simulated AI", "fail",
     "e2e_burst1000_20261103_181806",
     "Safety stop; 900 calls got in",
     "Server log: 'QueuePool limit of size 12 overflow 6 reached, connection timed out, timeout 30.00'; uploads/refresh 500, list 401",
     "The limit is the API's own pool (12+6 per process), not the database server — the DB change did not touch it",
     "Zips made bigger (fewer at once). Backend fix 2 still open."),
    (30, "3 Nov 18:36", "Burst · 1,000 calls (8×125)", "Lambda, simulated AI", "pass", "e2e_burst1000_20261103_183559",
     "", "5 website requests failed (502, under 1%); website p95 ~13 s; window looked empty",
     "Unpacking load; console output buffered", "Console now unbuffered. Website speed: backend fix 1."),
    (31, "3 Nov 18:52", "Burst · 100 calls (1×100)", "Lambda, simulated AI", "pass", "e2e_burst100_20261103_185132", "", "—", "—", "—"),
    (32, "3 Nov 18:56", "Burst · 200 calls (2×100)", "Lambda, simulated AI", "pass", "e2e_burst200_20261103_185605", "", "—", "—", "—"),
    (33, "3 Nov 19:02", "Burst · 500 calls (5×100)", "Lambda, simulated AI", "pass", "e2e_burst500_20261103_190120", "",
     "1 failed website request", "—", "—"),
    (34, "3 Nov 19:13", "Burst · 1,000 calls (10×100)", "Lambda, simulated AI", "pass", "e2e_burst1000_20261103_191123", "",
     "Analysis paused ~2 min, then 40–85/min", "Lambda/SQS backlog or throttling (unconfirmed)", "Open: check Lambda throttles (backend fix 5)."),
    (35, "3 Nov 20:20", "Burst · 1,000 calls (2×500)", "Lambda, simulated AI", "pass", "e2e_burst1000_20261103_202034", "",
     "Upload took 4:25; analysis paused ~2 min", "One upload stream per zip over office Wi-Fi (~0.7 MB/s); same analysis pause",
     "Use 8–10 zips per burst; backend fix 5."),
    (36, "3 Nov 23:13", "Step-up to 40,000/hour · 200 → 300 → 450 → 667 calls/min, 10 min each (zips of 100)", "Lambda, simulated AI",
     "fail", "e2e_step_20261103_231328",
     "Step 1 (200/min = 12,000/h) passed. Step 2 (300/min) failed after ~10 min: API 1,020 calls behind in unpacking; uploads stopped. "
     "4,900 calls sent, all created; analysis ~90/min. QAT then went down at ~00:00 IST while catching up",
     "Call creation fell from ~290 to ~125/min at 300/min; website 95% within 7.5 s; later HTTP 502/504/503 for the whole site",
     "Unpacking runs in the web worker and competes for the 18 DB connections; analysis limited by Lambda concurrency; "
     "outage cause not confirmed (no test load at the time)",
     "Limit found: ~200 calls/min accepted, ~90/min analysed. Backend fixes 1–5 and 13 needed for 40,000/h."),
]

ERRORS = [
    # (error seen, where, root cause, fix, status)
    ("HTTP 500 \"Authentication service temporarily unavailable\" on upload and token refresh; 401 on pages",
     "Runs 24, 28, 29 (1,000 calls in 40 zips)",
     "Every zip being unpacked holds a database connection; 40 at once used up the API's pool of 18 (12 + 6), so logins could not get one",
     "Test: ≤ 10 zips per burst. Backend: limit concurrent unpacking and raise DB_POOL_SIZE / DB_MAX_OVERFLOW", "open"),
    ("sqlalchemy TimeoutError: QueuePool limit of size 12 overflow 6 reached",
     "Server log, run 29", "Same as above (confirmed in server log, raised in _resolve_group / process_audio)", "Same as above", "open"),
    ("HTTP 504 on calls list, then HTTP 503 for the whole QAT site",
     "Run 19", "API server overloaded (exact cause not confirmed)", "Safety stop added to the test; server-side cause to be checked by DevOps", "open"),
    ("Whole QAT site HTTP 502/504 then 503 from the load balancer at ~00:00 IST", "Run 36 (while analysing the backlog, no uploads)",
     "No healthy servers behind the load balancer; cause not confirmed (midnight job/restart, disk full from zips, or DB connections)",
     "Safety stop ended tracking; DevOps to check target health, gunicorn log, disk and RDS connections", "open"),
    ("API falls behind at 300 calls/min (1,020 calls waiting to be unpacked)", "Run 36, step 2",
     "Unpacking in the web worker slows down as zips pile up and compete for the 18 DB connections", "Backend fixes 1–4 (queue worker, pool, queries)", "open"),
    ("Website p95 4–14 s during bursts", "Runs 23, 27, 30, 34, 35",
     "Zip unpacking runs inside the same web worker processes that serve the website", "Move unpacking to a separate queue worker", "open"),
    ("Analysis pauses ~2 min at the start of large bursts; 40–90 calls/min ceiling", "Runs 34, 35",
     "Scoring Lambdas limited to 15 at once; possible throttling (unconfirmed)", "Check CloudWatch Throttles / SQS age; raise reserved concurrency", "open"),
    ("Calls permanently stuck (4, later 8)", "Runs 3, 4 onwards",
     "Pipeline step failed and message went to a dead-letter queue (unconfirmed)", "Inspect the DLQs and partial-failure rows", "open"),
    ("Celery analysis ~1.1 calls/min (≈70 h for 5,000)", "Runs 1, 2", "Single shared Celery queue", "QAT moved to the Lambda pipeline", "fixed"),
    ("'certificate signed by unknown authority', connect timeouts", "Run 3", "TLS inspection on the test PC (unconfirmed)", "Did not recur", "closed"),
    ("HTTP 429 'Please wait N second(s) before requesting a new OTP' / invalid OTP", "Before run 1, runs 7–8",
     "One OTP per minute; OTP typed before the prompt", "Visible prompt, automatic wait before retry", "fixed"),
    ("Test kept running after all calls finished", "Run 13", "Fixed 5-minute browse window", "Test stops itself when done", "fixed"),
    ("Sequence ended by one Ctrl+C", "Run 15", "Ctrl+C used to copy text", "Second Ctrl+C required", "fixed"),
    ("Test lost when the Claude app crashed", "Runs 6, 18", "Test ran inside Claude's terminal", "Tests run in their own window with a log", "fixed"),
    ("Tracker crashed on a timeout, safety stop did not fire", "Run 24", "Unhandled network error", "All network errors count toward the safety stop", "fixed"),
    ("Empty test window", "Run 30", "Output buffered by the log pipe", "Unbuffered output", "fixed"),
]

TOOLING = [
    ("Windows console crashed printing → — ≥", "ASCII text / UTF-8 mode"),
    ("Wrong BASE_URL and inline comments read as values in perf.env", "URL corrected, parser ignores comments"),
    ("Finish check ignored deliberately skipped calls; wrong rates on stopped runs", "Counts analysed + skipped; correct live rates"),
    ("Re-used file names made the backend copy old results", "Unique file-name prefix per run"),
    ("Leftover calls skewed results", "Run waits until the group has nothing waiting"),
    ("Fixed browse window kept tests running", "Test stops itself when all calls are processed"),
    ("Single Ctrl+C stopped the sequence", "Second Ctrl+C required between bursts"),
    ("Claude crash killed runs", "Separate PowerShell window + log watcher"),
    ("Tracker thread crash disabled the safety stop", "All errors counted; 30 s timeout; stop after 4 failures"),
    ("Buffered console output", "Unbuffered Python output"),
    ("Watcher false alarm", "Watcher follows the process ID"),
    ("Report rounding, bar widths, repeated labels", "Fixed in the report generators"),
]

BACKEND = [
    # (n, finding, where, fix, effort, status)
    (1, "Zip unpacking runs as a BackgroundTask inside the web worker, with no limit on how many zips run at once",
     "calls_upload_script/service.py · handle_zip_upload / _extract_and_process_zip",
     "Move to a queue worker (Celery/SQS) with fixed concurrency; quick interim fix: asyncio.Semaphore around unpacking",
     "Medium (interim: small)", "open"),
    (2, "API database pool of 12 + 6 per process is exhausted by ~18 concurrent zips → login errors for everyone",
     "app/db/connection.py · DB_POOL_SIZE / DB_MAX_OVERFLOW",
     "Raise via env (e.g. 30 + 10), keeping pool × processes + Lambda connections under the database max_connections",
     "Small (config)", "open"),
    (3, "Database connection held during each S3 upload", "service.py · process_audio, Lambda branch",
     "Close the session before the S3 upload, reopen for pipeline_start", "Small", "open"),
    (4, "Per-call MAX(display_id) and leading-% LIKE duplicate check get slower as data grows; display_id not unique",
     "service.py · _next_display_id, _get_or_create_call_inner", "Postgres sequence + indexed (group_id, file name) column (migration)",
     "Medium", "open"),
    (5, "Analysis ceiling ~45–90 calls/min, pauses at burst start", "Lambda reserved concurrency (AWS)",
     "Check Throttles / SQS age; raise scoring Lambda concurrency (adds cost and DB connections)", "Small (config)", "open"),
    (6, "Calls stuck permanently", "Lambda DLQs", "Inspect and redrive DLQ messages; alarm on DLQ depth", "Small", "open"),
    (7, "Every request reloads the user + permissions from the database", "app/common/dependencies.py · get_current_user",
     "Short-lived cache (e.g. Redis) for the user/permission lookup", "Medium", "open"),
    (8, "Unfinished zips are lost if a web worker restarts (deploy, timeout)", "BackgroundTask in gunicorn",
     "Resolved by fix 1 (durable queue)", "—", "open"),
    (9, "Cross-tenant upload via tenant_id form field", "calls_upload_script/router.py · _resolve_tenant_id",
     "Fixed locally with unit test", "Done locally", "not deployed"),
    (10, "Upload Celery tasks expired after 1 h (silently dropped)", "service.py apply_async(expires=3600)",
     "Removed on upload path locally; webhook paths still have it", "Done locally", "not deployed"),
    (11, "QAT sits behind a production-named load balancer", "AWS ALB", "Confirm with DevOps that QAT and production are isolated", "—", "open"),
    (12, "Session logout raised from 60 to 180 min for testing", "App setting", "Revert after testing (security)", "Small", "open"),
    (13, "QAT went down (503 for the whole site) twice after load had stopped: 3 Nov ~03:00 and ~00:00 IST", "AWS ALB / EC2 / RDS",
     "Find the cause (target health, gunicorn-qat log, disk space, RDS connections, scheduled jobs) and add CloudWatch alarms",
     "Small–medium", "open"),
    (14, "Lambdas open one DB connection each (NullPool); raising concurrency for 40,000/h would exceed database connections",
     "Call-Pipeline Lambdas · RDS", "Add RDS Proxy before raising reserved concurrency", "Medium (infra, adds cost)", "open"),
]

STATUS = {"pass": ("pass", "Passed"), "fail": ("fail", "Failed"), "stop": ("warn", "Stopped"), "abort": ("info", "Did not start")}
ESTATUS = {"open": ("fail", "Open"), "fixed": ("pass", "Fixed"), "closed": ("info", "Closed"), "not deployed": ("warn", "Not deployed")}

CSS = """
.hist .table-wrap{max-height:none}.hist td{white-space:normal;vertical-align:top;font-size:13px}.hist th{font-size:12px}
.hist td.n{color:var(--text-3);font-variant-numeric:tabular-nums}.hist .sm{font-size:12px;color:var(--text-3);display:block;margin-top:2px}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.toc a{font-size:13px;color:#fff;text-decoration:none;border:1px solid rgba(255,255,255,.4);
border-radius:999px;padding:5px 12px}
"""


def load(stem):
    if not stem:
        return None
    try:
        return json.loads((HERE / "qat" / "results" / f"{stem}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def measured(r):
    """Short measured line for runs with a result file."""
    if not r:
        return ""
    b = B.burst_numbers(r) if (r.get("plan") or {}).get("burst") else None
    k = r.get("k6") or {}
    p95 = k.get("browse_p95_ms")
    parts = [f"{r.get('uploaded_calls') or 0:,} calls sent"]
    if b and b["total_s"]:
        parts.append(f"done in {mins(b['total_s'])}")
    parts.append(f"{(r.get('final') or {}).get('processed', 0):,} processed")
    if p95:
        parts.append(f"website p95 {p95 / 1000:.1f} s")
    return " · ".join(parts)


def render():
    rows, counts = [], {"pass": 0, "fail": 0, "stop": 0, "abort": 0}
    for n, when, test, pipe, st, stem, outcome, prob, cause, action in RUNS:
        counts[st] += 1
        m = measured(load(stem))
        out = outcome or ("All calls processed" if st == "pass" else "")
        rows.append(f"<tr><td class=n>{n}</td><td>{e(when)}</td><td><b>{e(test)}</b><span class=sm>{e(pipe)}</span></td>"
                    f"<td>{R.badge(*STATUS[st])}</td><td>{e(out)}{f'<span class=sm>{e(m)}</span>' if m else ''}</td>"
                    f"<td>{e(prob)}</td><td>{e(cause)}</td><td>{e(action)}</td></tr>")
    err_rows = "".join(
        f"<tr><td><b>{e(a)}</b></td><td>{e(w)}</td><td>{e(c)}</td><td>{e(f)}</td><td>{R.badge(*ESTATUS[s])}</td></tr>"
        for a, w, c, f, s in ERRORS)
    tool_rows = "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td><td>{R.badge('pass', 'Fixed')}</td></tr>" for a, b in TOOLING)
    be_rows = "".join(
        f"<tr><td class=n>{n}</td><td><b>{e(f)}</b></td><td><code>{e(w)}</code></td><td>{e(x)}</td><td>{e(eff)}</td>"
        f"<td>{R.badge(*ESTATUS[s])}</td></tr>" for n, f, w, x, eff, s in BACKEND)
    total = len(RUNS)
    cards = "".join([
        B.kpi("Test runs attempted", str(total), "runs", "Every run started on QAT between 28 Oct and 4 Nov, including ones that were stopped early.",
              "Every practice service the kitchen held, good or bad.", "neutral"),
        B.kpi("Completed successfully", str(counts["pass"]), "runs", "All uploaded calls were processed.", "Services where every order was served.", "good"),
        B.kpi("System failures", str(counts["fail"]), "runs", "QAT returned errors, stopped responding, or left calls unprocessed.",
              "Services where the kitchen was overwhelmed.", "bad"),
        B.kpi("Stopped / did not start", str(counts["stop"] + counts["abort"]), "runs",
              "Ended by us, by a tooling problem or a login issue — not a verdict on the system.", "Services cancelled for reasons outside the kitchen.", "warn"),
        B.kpi("Open backend issues", str(sum(1 for b in BACKEND if b[5] == "open")), "items", "Recommended code / configuration changes not yet made.",
              "Repairs the kitchen still needs.", "warn"),
        B.kpi("Tooling problems fixed", str(len(TOOLING)), "items", "Issues in the test scripts found and fixed during testing.",
              "Fixes to the practice equipment, not the kitchen.", "good"),
    ])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Run History</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>{R.CSS}{B.EXTRA_CSS}{CSS}{S.CSS}</style></head><body><main class="wrap hist">
<header class="hero"><div class="eyebrow">BPO-BOX · QAT performance testing</div>
<h1>Every test run, what went wrong, and what we did about it</h1>
<p>All {total} test runs attempted on the QAT environment from 28 October to 4 November 2026 — passed, failed, stopped and aborted — with
every error seen, its root cause, the action taken, and the backend issues still open.</p>
<span class="pill">Environment: qat.bpobox.ai</span><span class="pill">28 Oct – 4 Nov 2026 (IST)</span>
<span class="pill">{counts['pass']} passed · {counts['fail']} failed · {counts['stop'] + counts['abort']} stopped</span>
<nav class="toc"><a href="#limits">Limits found</a><a href="#compare">Scenarios compared</a><a href="#verdict">Good / to improve</a><a href="#runs">All runs</a><a href="#errors">Errors &amp; fixes</a><a href="#backend">Backend fixes needed</a><a href="#tooling">Test tooling fixes</a></nav></header>

<section><div class="section-head"><h2>Overview</h2></div><div class="bks">{cards}</div></section>

<section id="limits"><div class="section-head"><h2>Limits found</h2><p>What QAT can handle today, from the step-up test and the bursts</p></div><div class="bks">{S.limits_cards()}</div></section>

<section id="compare"><div class="section-head"><h2>Every scenario compared</h2><p>Passed, failed and stopped, side by side</p></div>{S.compare_table(include_failed=True)}</section>

<section id="verdict"><div class="section-head"><h2>What is good and what needs improvement</h2><p>Across all runs</p></div>{S.good_improve_html()}</section>

<section id="runs"><div class="section-head"><h2>All test runs</h2><p>In the order they were run · times in IST</p></div>
<div class="card table-wrap"><table class="btable"><thead><tr><th>#</th><th>When</th><th>Test</th><th>Result</th><th>Outcome</th>
<th>Problem seen</th><th>Root cause</th><th>Action taken / status</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="tbl-note">"Stopped" means the run was ended by us, by a test-tooling issue or on request; it is not a verdict on the system.
Burst sizes show zips × calls per zip (for example 8×125 = 8 zips of 125 calls). Measured figures come from each run's result file.</p></div></section>

<section id="errors"><div class="section-head"><h2>Errors and fixes</h2><p>Each distinct problem, once</p></div>
<div class="card table-wrap"><table class="btable"><thead><tr><th>Error seen</th><th>Runs</th><th>Root cause</th><th>Fix</th><th>Status</th></tr></thead>
<tbody>{err_rows}</tbody></table></div></section>

<section id="backend"><div class="section-head"><h2>Backend and infrastructure fixes needed</h2><p>In order of impact</p></div>
<div class="card table-wrap"><table class="btable"><thead><tr><th>#</th><th>Finding</th><th>Where</th><th>Recommended fix</th><th>Effort</th><th>Status</th></tr></thead>
<tbody>{be_rows}</tbody></table>
<p class="tbl-note">Rollback for configuration changes (2, 5): restore the previous values and restart. Code changes (1, 3, 4, 7) should be tested on QAT
with a 1,000-call burst in 40 small zips — the case that failed — before release.</p></div></section>

<section id="tooling"><div class="section-head"><h2>Test tooling fixes</h2><p>Problems in the test scripts, all fixed during testing</p></div>
<div class="card table-wrap"><table class="btable"><thead><tr><th>Problem</th><th>Fix</th><th>Status</th></tr></thead><tbody>{tool_rows}</tbody></table></div></section>

<footer>BPO-BOX performance testing · run history · k6 load generator + system progress checks</footer>
</main><div id="tip" hidden></div><script>{R.JS}</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, default=HERE / "qat" / "results" / "BPO-BOX_Test_Run_History.html")
    args = ap.parse_args()
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(), encoding="utf-8")
    except OSError as err:
        print(f"Cannot write {args.out}: {err}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

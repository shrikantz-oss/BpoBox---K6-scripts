"""Cross-check one test run against QAT's own data, call by call.

The run report counts calls from the test group's totals. This script instead lists the calls the run
created and compares count, status and timing with what the report says. Read-only: it only lists calls.

How a run's calls are found. QAT does not keep the uploaded file name (the recording path becomes an S3 key
built from the call's own ID), and the zip upload BACK-DATES each call's created_at by (index in zip % 60)
days (calls_upload_script.get_cycled_created_at), keeping the time of day. So the whole test group is read
once, and a run's calls are the uploads (call_id "UPLOAD-…") whose created_at, moved forward by 0-59 whole
days, falls inside the run's window (start → last progress sample). Earlier runs at the same time of day
on other days are excluded by display number: a run's first call (index 0, never back-dated) has the
lowest display number of the run. Nobody else should upload into the test group during a run.

  python -I verify_run.py qat/results/e2e_burst1000_20261103_183559.json
  python -I verify_run.py <result.json> --env-file qat/perf.env --tolerance 60

Writes <result>_verify.md and <result>_verify.json next to the result file.
"""
import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import e2e_report as R  # noqa: E402
from run_e2e import Tracker  # noqa: E402
from run_e2e import call_state as classify, display_num  # noqa: E402  (shared rules for call status and numbering)
from run_perf import ApiError, load_env_file, login  # noqa: E402

PAGE_SIZE = 100  # API maximum
PAGE_TRIES, PAGE_RETRY_S = 4, 15  # per list page: 1 try + 3 retries, 15 s apart


def parse_ts(s, offset_min):
    """API timestamp → aware UTC datetime (naive values are taken as UTC + offset_min)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(minutes=offset_min)))
    return dt.astimezone(timezone.utc)


BACKDATE_DAYS = 60  # get_cycled_created_at: created_at = now - (index % 60) days
_group_cache = {}  # group_id -> (items, pages, complete): read once per process, shared by all runs checked


def fetch_group_calls(tracker, group_id, max_pages):
    """Every call in the group (back-dating scatters a run's calls over 60 days, so all pages are needed)."""
    if group_id in _group_cache:
        return _group_cache[group_id]
    items, page, complete = [], 1, False
    while page <= max_pages:
        path = f"/qa/calls?group_ids={group_id}&page={page}&page_size={PAGE_SIZE}&sort_by=date:desc"
        for attempt in range(1, PAGE_TRIES + 1):  # a busy QAT can take > 30 s on one page: retry before giving up
            try:
                body = tracker._get(path)
                break
            except ApiError as e:
                if attempt == PAGE_TRIES:
                    raise
                print(f"  page {page} failed ({e}); retrying in {PAGE_RETRY_S} s ({attempt}/{PAGE_TRIES - 1})", flush=True)
                time.sleep(PAGE_RETRY_S)
        data = body.get("data") or {}
        batch = data.get("items") or []
        items += batch
        pages = (data.get("pagination") or {}).get("total_pages")
        if page % 20 == 0:
            print(f"  read {len(items):,} calls ({page}/{pages or '?'} pages) ...", flush=True)
        if not batch or (pages and page >= pages):
            complete = True
            break
        page += 1
        time.sleep(0.2)  # be gentle with a shared test system
    _group_cache[group_id] = (items, min(page, max_pages), complete)
    return _group_cache[group_id]




def match_run(items, win_start, win_end, offset_min):
    """[(item, real created time, days back-dated)] for uploads created inside the window."""
    matches = []
    for it in items:
        if not str(it.get("call_id") or "").startswith("UPLOAD-"):
            continue
        c = parse_ts(it.get("created_at"), offset_min)
        if not c:
            continue
        for k in range(BACKDATE_DAYS):
            t = c + timedelta(days=k)
            if t > win_end:
                break
            if t >= win_start:
                matches.append((it, t, k))
                break
    # Same time of day on an earlier day = an earlier run: drop anything numbered before this run's first call
    anchors = [n for it, _, k in matches if k == 0 and (n := display_num(it)) is not None]
    if anchors:
        lo = min(anchors)
        matches = [m for m in matches if (display_num(m[0]) or lo) >= lo]
    return matches


def verify(r, cfg, tracker, tolerance, offset_min, max_pages):
    run_id, t0 = r["run_id"], r["t0_epoch"]
    start = datetime.fromtimestamp(t0, timezone.utc)
    tl = r.get("timeline") or []
    # Window: test start (small clock margin) → last progress sample (+30 s); the next burst starts ≥ 2 min later.
    win_start = start - timedelta(seconds=10)
    win_end = start + timedelta(seconds=(tl[-1]["t"] if tl else 3600) + 30)
    print(f"Finding uploads created {win_start:%H:%M:%S}–{win_end:%H:%M:%S} UTC on {start:%Y-%m-%d} "
          f"in group {cfg['GROUP_ID']} (run {run_id}) ...")
    items, pages, complete = fetch_group_calls(tracker, cfg["GROUP_ID"], max_pages)
    scanned = len(items)
    matches = match_run(items, win_start, win_end, offset_min)
    print(f"Scanned {scanned:,} calls on {pages} page(s){'' if complete else ' (STOPPED AT --max-pages)'}; "
          f"{len(matches):,} were created by this run.")

    calls, created, updated = {}, [], []
    for i, (it, real_created, k) in enumerate(matches):
        calls[it.get("id") or i] = it  # one record per call
        created.append(real_created)
        if classify(it) in ("analysed", "skipped"):
            u = parse_ts(it.get("updated_at"), offset_min)
            if u and u < win_start:  # never updated since the back-dated insert: undo the same shift
                u += timedelta(days=k)
            if u:
                updated.append(u)
    status = Counter(classify(it) for it in calls.values())

    d, fin = R.derive(r), r.get("final") or {}
    rel = lambda dt: (dt - start).total_seconds() if dt else None  # noqa: E731  seconds from test start
    rows = []

    def row(check, rep, qat, ok, note=""):
        rows.append({"check": check, "report": rep, "qat": qat, "match": ok, "note": note})

    up = r.get("uploaded_calls") or 0
    note = "k6 counted HTTP 200 uploads × calls per zip; QAT = uploaded calls created in the run window"
    if not calls and offset_min == 0:
        note += ". None found: if QAT returns local times without a zone, rerun with --tz-offset-min (330 for IST)"
    elif len(calls) > up:
        note += ". More than uploaded: duplicates, or someone else uploaded into the group during the run"
    row("Calls created in QAT", up, len(calls), len(calls) == up, note)
    qat_done = status["analysed"] + status["skipped"]
    row("Calls finished (analysed + skipped)", f"{fin.get('processed', 0):,} (group total)", qat_done, qat_done == up,
        "the report counts the whole group, so leftovers from earlier runs can push it above the upload count"
        if fin.get("processed", 0) > up else "")
    row("  analysed", f"{fin.get('scored', 0):,} (group total)", status["analysed"], None)
    row("  skipped on purpose (short / sampling)", f"{fin.get('skipped', 0):,} (group total)", status["skipped"], None)
    row("Calls failed in the pipeline", "–", status["failed"], status["failed"] == 0)
    row("Calls still in progress", "–", status["in_progress"], status["in_progress"] == 0)

    counts_ok = all(x["match"] is not False for x in rows)  # every row so far is a count

    def timing(check, rep_s, qat_dt):
        q = rel(qat_dt)
        ok = None if rep_s is None or q is None else abs(q - rep_s) <= tolerance
        note = ""
        if ok is False and offset_min == 0 and abs(q - rep_s) >= 1700:
            note = "differs by about a whole time zone: rerun with --tz-offset-min if QAT returns local times"
        elif ok is False and counts_ok and q > rep_s and fin.get("processed", 0) > up:
            note = ("the report called it done early: leftover calls from earlier runs finished during this burst and "
                    "filled the group total; QAT's time is the real one")
        row(check, R._dur(rep_s) if rep_s is not None else "–", R._dur(q) if q is not None else "–", ok,
            note or f"seconds from test start; tolerance ±{tolerance}s")
        return q

    timing("First call created", None, min(created) if created else None)
    last_created = timing("Last call created", d["t_all_ingested"], max(created) if created else None)
    last_done = timing("Last call finished", d["t_all_processed"], max(updated) if updated else None)
    # match: everything agrees · time_corrected: every call is there and finished, only the report's times were off
    verdict = ("match" if all(x["match"] is not False for x in rows)
               else "time_corrected" if counts_ok and status["in_progress"] == 0 else "mismatch")
    return {"run_id": run_id, "result": r.get("_path"), "verified_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_url": cfg["BASE_URL"], "group_id": cfg["GROUP_ID"], "scanned": scanned, "found": len(calls),
            "status": dict(status), "rows": rows, "verdict": verdict,
            "qat_last_created_s": last_created, "qat_last_finished_s": last_done}


def verify_summary(v, md_name):
    """What a run's result file keeps about its verification (read by burst_summary.py)."""
    return {k: v[k] for k in ("verdict", "found", "status", "qat_last_created_s", "qat_last_finished_s")} | {"file": md_name}


def verify_and_save(res, cfg, tracker, poll_s):
    """Check a just-finished run call by call and record the outcome next to it and in its result file.
    Used by run_burst.py and run_e2e.py right after a run; never raises for API problems."""
    path = Path(res["result_file"])
    _group_cache.clear()  # the group changed during the run: read it fresh
    try:
        v = verify({**res, "_path": str(path)}, cfg, tracker, tolerance=max(60, 2 * poll_s),
                   offset_min=int(cfg.get("VERIFY_TZ_OFFSET_MIN", "0")), max_pages=600)
    except ApiError as e:
        print(f"Per-call verification could not run: {e}", file=sys.stderr)
        res["verify"] = {"verdict": "error", "error": str(e)}
    else:
        stem = path.with_suffix("")
        Path(f"{stem}_verify.md").write_text(to_markdown(v, res), encoding="utf-8")
        Path(f"{stem}_verify.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
        res["verify"] = verify_summary(v, f"{stem.name}_verify.md")
        bad = [x["check"].strip() for x in v["rows"] if x["match"] is False]
        word = {"match": "all checks match", "time_corrected": "all calls OK, report time corrected: " + ", ".join(bad)}
        print(f"Per-call verification: {v['found']:,} calls found · "
              f"{word.get(v['verdict'], 'MISMATCH: ' + ', '.join(bad))} · {stem.name}_verify.md")
    try:
        path.write_text(json.dumps(res, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"Could not save the verification into {path.name}: {e}", file=sys.stderr)


def to_markdown(v, r):
    mark = {True: "✅", False: "❌", None: "–"}
    lines = [f"# Run verification: {r.get('scenario')} · {v['run_id']}", "",
             f"- Result file: `{Path(v['result']).name}`", f"- Checked against: {v['base_url']} (group `{v['group_id']}`)",
             f"- Checked at: {v['verified_utc']} · {v['scanned']:,} calls scanned, {v['found']:,} belong to this run",
             "- **Verdict: " + {"match": "all checks match",
                                 "time_corrected": "every call present and finished; the report's timing was off, use QAT's time (❌ rows)",
                                 }.get(v["verdict"], "MISMATCH, see ❌ rows") + "**", "",
             "| Check | Report says | QAT says | Match | Note |", "|---|---|---|---|---|"]
    for x in v["rows"]:
        rep = f"{x['report']:,}" if isinstance(x["report"], int) else x["report"]
        qat = f"{x['qat']:,}" if isinstance(x["qat"], int) else x["qat"]
        lines.append(f"| {x['check']} | {rep} | {qat} | {mark[x['match']]} | {x['note']} |")
    lines += ["", "How to read this: the report measures from outside (k6 uploads, group totals polled every few seconds). "
              "QAT's side is every uploaded call (ID UPLOAD-…) created in the test group during this run's window, read "
              "from the same Call Review list users see. QAT doesn't keep uploaded file names and back-dates each "
              "uploaded call by 0-59 days (keeping the time of day), so calls are matched by time of day, and earlier "
              "runs are excluded by display number. 'Last call finished' uses each call's last update time, so a later "
              "manual review of a call would move it."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", type=Path, nargs="+", help="one or more results/e2e_*.json files (one login for all)")
    ap.add_argument("--env-file", type=Path, default=HERE / "qat" / "perf.env")
    ap.add_argument("--tolerance", type=int, default=60, help="allowed timing difference in seconds (default 60)")
    ap.add_argument("--tz-offset-min", type=int, default=0, help="minutes to add if QAT returns times without a zone")
    ap.add_argument("--max-pages", type=int, default=400, help=f"safety cap on list pages of {PAGE_SIZE} calls")
    args = ap.parse_args()

    cfg = load_env_file(args.env_file)
    for key in ("BASE_URL", "PERF_EMAIL", "GROUP_ID"):
        if not cfg.get(key):
            print(f"{key} is required in {args.env_file}", file=sys.stderr)
            return 2
    base = cfg["BASE_URL"].rstrip("/")

    runs = []  # check every file before logging in, so a typo doesn't waste an OTP
    for path in args.result:
        if path.name.endswith(("_k6.json", "_verify.json")):
            continue
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            print(f"Cannot read {path}: {err}", file=sys.stderr)
            return 2
        if not r.get("run_id") or not r.get("t0_epoch"):
            print(f"{path.name}: not a run result (needs run_id and t0_epoch).", file=sys.stderr)
            return 2
        if r.get("base_url") and r["base_url"].rstrip("/") != base:
            print(f"{path.name} ran against {r['base_url']} but {args.env_file} points at {base}.", file=sys.stderr)
            return 2
        if r.get("group_id") and r["group_id"] != cfg["GROUP_ID"]:
            print(f"{path.name} used group {r['group_id']} but {args.env_file} has {cfg['GROUP_ID']}.", file=sys.stderr)
            return 2
        r["_path"] = str(path)
        runs.append((path, r))
    if not runs:
        print("No run result files given.", file=sys.stderr)
        return 2

    try:
        token, refresh = login(base, cfg["PERF_EMAIL"], cfg.get("FORCE_LOGIN", "").lower() == "true")
    except (ApiError, KeyboardInterrupt) as err:
        print(f"Login stopped: {err or 'interrupted'}", file=sys.stderr)
        return 1
    tracker = Tracker(base, token, refresh, cfg["GROUP_ID"])

    worst = 0
    for path, r in runs:
        try:
            v = verify(r, cfg, tracker, args.tolerance, args.tz_offset_min, args.max_pages)
        except (ApiError, KeyboardInterrupt) as err:
            print(f"Verification of {path.name} stopped: {err or 'interrupted'}", file=sys.stderr)
            return 1
        md = to_markdown(v, r)
        stem = path.with_suffix("")
        # Also record the outcome in the run's own result file, where burst_summary.py reads it
        saved = {k: val for k, val in r.items() if k != "_path"}
        saved["verify"] = verify_summary(v, f"{stem.name}_verify.md")
        try:
            Path(f"{stem}_verify.md").write_text(md, encoding="utf-8")
            Path(f"{stem}_verify.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
            path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        except OSError as err:
            print(f"Cannot write the verification files: {err}", file=sys.stderr)
            return 1
        print("\n" + md)
        print(f"Saved: {stem}_verify.md and {stem}_verify.json")
        worst = max(worst, 3 if v["verdict"] == "mismatch" else 0)
    return worst


if __name__ == "__main__":
    sys.exit(main())

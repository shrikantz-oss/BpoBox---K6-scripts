"""Cross-check one test run against QAT's own data, call by call.

The run report counts calls from the test group's totals. This script instead finds every call the
run created (each uploaded file name starts with the run's unique RUN_ID) in QAT's calls list and
compares count, status and timing with what the report says. Read-only: it only lists calls.

  python -I verify_run.py qat/results/e2e_burst1000_20261103_183559.json
  python -I verify_run.py <result.json> --env-file qat/perf.env --tolerance 60

Writes <result>_verify.md and <result>_verify.json next to the result file.
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import e2e_report as R  # noqa: E402
from run_e2e import Tracker  # noqa: E402
from run_perf import ApiError, load_env_file, login  # noqa: E402

DONE_STATES = {"not_reviewed", "not_eligible"}  # sampling skips: finished on purpose, like the tracker's "skipped"
PAGE_SIZE = 100  # API maximum


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


def classify(item):
    """analysed | skipped | failed | in_progress, from the fields the Call Review page uses."""
    if (item.get("qa_status") or "") == "failed":
        return "failed"
    if (item.get("review_state") or "") in DONE_STATES:
        return "skipped"
    if item.get("qa_status") == "completed" or item.get("score_percentage") is not None:
        return "analysed"
    return "in_progress"


def fetch_run_calls(tracker, group_id, run_id, since, max_pages):
    """Page the group's calls (newest first) and keep those whose recording name starts with run_id."""
    # <RUN_ID>_<zip, ≥6 digits>_<call in zip, ≥2 digits>.<ext> (e2e_scenarios.js pads, so _124 is valid for zips > 100)
    name_re = re.compile(rf"(?:^|/){re.escape(run_id)}_(\d{{6,}})_(\d{{2,}})\.[A-Za-z0-9]+(?:$|\?)")
    found, scanned, page = [], 0, 1
    while page <= max_pages:
        body = tracker._get(f"/qa/calls?group_ids={group_id}&page={page}&page_size={PAGE_SIZE}&sort_by=date:desc")
        data = body.get("data") or {}
        items = data.get("items") or []
        if not items:
            break
        scanned += len(items)
        for it in items:
            m = name_re.search(it.get("call_recording_file_path") or "")
            if m:
                found.append((m.group(1), m.group(2), it))
        # Newest first: once a whole page is older than the run (minus a margin), nothing further belongs to it.
        # Uses the page's NEWEST call, since "date" ordering need not follow creation time exactly.
        newest = max((parse_ts(it.get("created_at"), 0) for it in items if it.get("created_at")), default=None)
        if newest and newest < since:
            break
        pages = (data.get("pagination") or {}).get("total_pages")
        if pages and page >= pages:
            break
        page += 1
        time.sleep(0.2)  # be gentle with a shared test system
    return found, scanned, page


def verify(r, cfg, tracker, tolerance, offset_min, max_pages):
    run_id, t0 = r["run_id"], r["t0_epoch"]
    start = datetime.fromtimestamp(t0, timezone.utc)
    print(f"Finding calls of run {run_id} (started {start:%Y-%m-%d %H:%M} UTC) in group {cfg['GROUP_ID']} ...")
    found, scanned, pages = fetch_run_calls(tracker, cfg["GROUP_ID"], run_id, start - timedelta(minutes=30), max_pages)
    print(f"Scanned {scanned:,} calls on {pages} page(s); {len(found):,} belong to this run.")

    names = Counter((z, j) for z, j, _ in found)
    dupes = sum(n - 1 for n in names.values() if n > 1)
    calls = {}
    for z, j, it in found:  # one record per uploaded file (keep the first if the backend made duplicates)
        calls.setdefault((z, j), it)
    per_zip = Counter(z for z, _ in calls)
    cpz = r.get("calls_per_zip") or 0
    short_zips = {z: n for z, n in per_zip.items() if cpz and n < cpz}
    status = Counter(classify(it) for it in calls.values())
    created = [parse_ts(it.get("created_at"), offset_min) for it in calls.values()]
    updated = [parse_ts(it.get("updated_at"), offset_min) for it in calls.values()
               if classify(it) in ("analysed", "skipped")]
    created, updated = [c for c in created if c], [u for u in updated if u]

    d, fin, k = R.derive(r), r.get("final") or {}, r.get("k6") or {}
    rel = lambda dt: (dt - start).total_seconds() if dt else None  # noqa: E731  seconds from test start
    rows = []

    def row(check, rep, qat, ok, note=""):
        rows.append({"check": check, "report": rep, "qat": qat, "match": ok, "note": note})

    up = r.get("uploaded_calls") or 0
    row("Calls uploaded (accepted by the API)", up, len(calls), len(calls) == up,
        "k6 counted HTTP 200 uploads × calls per zip; QAT = distinct run files found")
    row("Zips with every call present", k.get("zips_uploaded", "–"), len(per_zip) - len(short_zips),
        not short_zips and len(per_zip) == k.get("zips_uploaded", len(per_zip)),
        f"incomplete zips: {', '.join(f'{z} ({n}/{cpz})' for z, n in sorted(short_zips.items()))}" if short_zips else "")
    row("Duplicate call records", 0, dupes, dupes == 0, "same file registered more than once")
    qat_done = status["analysed"] + status["skipped"]
    row("Calls finished (analysed + skipped)", f"{fin.get('processed', 0):,} (group total)", qat_done, qat_done == up,
        "the report counts the whole group, so leftovers from earlier runs can push it above the upload count"
        if fin.get("processed", 0) > up else "")
    row("  analysed", f"{fin.get('scored', 0):,} (group total)", status["analysed"], None)
    row("  skipped on purpose (short / sampling)", f"{fin.get('skipped', 0):,} (group total)", status["skipped"], None)
    row("Calls failed in the pipeline", "–", status["failed"], status["failed"] == 0)
    row("Calls still in progress", "–", status["in_progress"], status["in_progress"] == 0)

    def timing(check, rep_s, qat_dt):
        q = rel(qat_dt)
        ok = None if rep_s is None or q is None else abs(q - rep_s) <= tolerance
        note = ""
        if ok is False and offset_min == 0 and abs(q - rep_s) >= 1700:
            note = "differs by about a whole time zone: rerun with --tz-offset-min if QAT returns local times"
        row(check, R._dur(rep_s) if rep_s is not None else "–", R._dur(q) if q is not None else "–", ok,
            note or f"seconds from test start; tolerance ±{tolerance}s")

    timing("First call created", None, min(created) if created else None)
    timing("Last call created", d["t_all_ingested"], max(created) if created else None)
    timing("Last call finished", d["t_all_processed"], max(updated) if updated else None)
    return {"run_id": run_id, "result": r.get("_path"), "verified_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_url": cfg["BASE_URL"], "group_id": cfg["GROUP_ID"], "scanned": scanned, "found": len(calls),
            "status": dict(status), "rows": rows,
            "verdict": "match" if all(x["match"] is not False for x in rows) else "mismatch"}


def to_markdown(v, r):
    mark = {True: "✅", False: "❌", None: "–"}
    lines = [f"# Run verification: {r.get('scenario')} · {v['run_id']}", "",
             f"- Result file: `{Path(v['result']).name}`", f"- Checked against: {v['base_url']} (group `{v['group_id']}`)",
             f"- Checked at: {v['verified_utc']} · {v['scanned']:,} calls scanned, {v['found']:,} belong to this run",
             f"- **Verdict: {'all checks match' if v['verdict'] == 'match' else 'MISMATCH, see ❌ rows'}**", "",
             "| Check | Report says | QAT says | Match | Note |", "|---|---|---|---|---|"]
    for x in v["rows"]:
        rep = f"{x['report']:,}" if isinstance(x["report"], int) else x["report"]
        qat = f"{x['qat']:,}" if isinstance(x["qat"], int) else x["qat"]
        lines.append(f"| {x['check']} | {rep} | {qat} | {mark[x['match']]} | {x['note']} |")
    lines += ["", "How to read this: the report measures from outside (k6 uploads, group totals polled every few seconds). "
              "QAT's side is every call whose recording file name starts with this run's unique ID, read from the same "
              "Call Review list users see. 'Last call finished' uses each call's last update time, so a later manual "
              "review of a call would move it."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", type=Path, help="a results/e2e_*.json file (not *_k6.json)")
    ap.add_argument("--env-file", type=Path, default=HERE / "qat" / "perf.env")
    ap.add_argument("--tolerance", type=int, default=60, help="allowed timing difference in seconds (default 60)")
    ap.add_argument("--tz-offset-min", type=int, default=0, help="minutes to add if QAT returns times without a zone")
    ap.add_argument("--max-pages", type=int, default=400, help=f"safety cap on list pages of {PAGE_SIZE} calls")
    args = ap.parse_args()

    try:
        r = json.loads(args.result.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Cannot read {args.result}: {err}", file=sys.stderr)
        return 2
    if not r.get("run_id") or not r.get("t0_epoch"):
        print("Not a run result (needs run_id and t0_epoch; pass e2e_<scenario>_<time>.json, not *_k6.json).", file=sys.stderr)
        return 2
    r["_path"] = str(args.result)
    cfg = load_env_file(args.env_file)
    for key in ("BASE_URL", "PERF_EMAIL", "GROUP_ID"):
        if not cfg.get(key):
            print(f"{key} is required in {args.env_file}", file=sys.stderr)
            return 2
    base = cfg["BASE_URL"].rstrip("/")
    if r.get("base_url") and r["base_url"].rstrip("/") != base:
        print(f"This run was against {r['base_url']} but {args.env_file} points at {base}. Use the matching env file.", file=sys.stderr)
        return 2
    if r.get("group_id") and r["group_id"] != cfg["GROUP_ID"]:
        print(f"This run used group {r['group_id']} but {args.env_file} has {cfg['GROUP_ID']}.", file=sys.stderr)
        return 2

    try:
        token, refresh = login(base, cfg["PERF_EMAIL"], cfg.get("FORCE_LOGIN", "").lower() == "true")
        v = verify(r, cfg, Tracker(base, token, refresh, cfg["GROUP_ID"]), args.tolerance, args.tz_offset_min, args.max_pages)
    except (ApiError, KeyboardInterrupt) as err:
        print(f"Verification stopped: {err or 'interrupted'}", file=sys.stderr)
        return 1

    md = to_markdown(v, r)
    stem = args.result.with_suffix("")
    try:
        Path(f"{stem}_verify.md").write_text(md, encoding="utf-8")
        Path(f"{stem}_verify.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
    except OSError as err:
        print(f"Cannot write the verification files: {err}", file=sys.stderr)
        return 1
    print("\n" + md)
    print(f"Saved: {stem}_verify.md and {stem}_verify.json")
    return 0 if v["verdict"] == "match" else 3


if __name__ == "__main__":
    sys.exit(main())

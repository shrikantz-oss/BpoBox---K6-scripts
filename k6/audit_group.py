"""Audit every call in the test group: what finished, what failed, what is stuck, and which run it came from.

Reads the whole group once (read-only), then
  1. counts all calls by outcome (analysed / skipped / failed / in progress) and pipeline status,
  2. lists every unfinished call (display ID, pipeline status, times),
  3. for each run result given, matches that run's calls (same rules as verify_run.py) and counts
     how many of them finished, failed or are still in progress.

  python -I audit_group.py qat/results/e2e_*_20261104_*.json
  python -I audit_group.py --env-file qat/perf.env            # group totals only

Writes qat/results/group_audit_<ts>.md (and .json).
"""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import verify_run as V  # noqa: E402
from run_e2e import Tracker  # noqa: E402
from run_perf import ApiError, load_env_file, login  # noqa: E402

UNFINISHED = ("in_progress", "failed")


def per_run(results, items, offset_min):
    """[(label, uploaded, Counter of outcomes)] for each run result, matched by its time window."""
    rows = []
    for r in sorted(results, key=lambda x: x.get("t0_epoch", 0)):
        start = datetime.fromtimestamp(r["t0_epoch"], timezone.utc)
        tl = r.get("timeline") or []
        win_end = start + timedelta(seconds=(tl[-1]["t"] if tl else 3600) + 30)
        matches = V.match_run(items, start - timedelta(seconds=10), win_end, offset_min)
        seen = {it.get("id") or i: it for i, (it, _, _) in enumerate(matches)}
        rows.append((f"{r.get('scenario')} · {start:%d %b %H:%M} UTC", r.get("uploaded_calls") or 0,
                     Counter(V.classify(it) for it in seen.values()), r.get("_path")))
    return rows


def render(items, complete, runs, base, group):
    outcome = Counter(V.classify(it) for it in items)
    uploads = [it for it in items if str(it.get("call_id") or "").startswith("UPLOAD-")]
    pipe = Counter((it.get("pipeline_overall_status") or "none") for it in items if V.classify(it) in UNFINISHED)
    lines = ["# Test group audit", "",
             f"- Group `{group}` on {base} · read {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
             + ("" if complete else " · ⚠️ STOPPED AT --max-pages, counts are partial"),
             f"- **{len(items):,} calls** ({len(uploads):,} from zip uploads)", "",
             "## All calls in the group", "", "| Outcome | Calls |", "|---|---|"]
    lines += [f"| {k} | {outcome.get(k, 0):,} |" for k in ("analysed", "skipped", "failed", "in_progress")]
    unfinished = [it for it in items if V.classify(it) in UNFINISHED]
    lines += ["", f"**Not finished: {len(unfinished):,}** (failed {outcome.get('failed', 0):,}, "
              f"still in progress {outcome.get('in_progress', 0):,})"]
    if pipe:
        lines += ["", "Pipeline status of the unfinished calls:", "", "| pipeline_overall_status | Calls |", "|---|---|"]
        lines += [f"| {k} | {n:,} |" for k, n in pipe.most_common()]
    if runs:
        lines += ["", "## Test runs, call by call", "",
                  "| Run | Uploaded | Found in QAT | Analysed | Skipped | Failed | Still in progress | All done? |",
                  "|---|---|---|---|---|---|---|---|"]
        for label, up, c, _ in runs:
            found = sum(c.values())
            ok = found == up and not c.get("failed") and not c.get("in_progress")
            lines.append(f"| {label} | {up:,} | {found:,} | {c.get('analysed', 0):,} | {c.get('skipped', 0):,} | "
                         f"{c.get('failed', 0):,} | {c.get('in_progress', 0):,} | {'✅' if ok else '❌'} |")
    if unfinished:
        lines += ["", "## Unfinished calls (oldest update first)", "",
                  "| Display ID | Call ID | Outcome | qa_status | pipeline_overall_status | created_at (as stored) | updated_at |",
                  "|---|---|---|---|---|---|---|"]
        for it in sorted(unfinished, key=lambda x: x.get("updated_at") or ""):
            lines.append(f"| {it.get('display_id') or '–'} | {it.get('call_id') or '–'} | {V.classify(it)} | "
                         f"{it.get('qa_status') or '–'} | {it.get('pipeline_overall_status') or '–'} | "
                         f"{(it.get('created_at') or '')[:19]} | {(it.get('updated_at') or '')[:19]} |")
    lines += ["", "Notes: uploaded calls are stored with created_at back-dated 0-59 days (keeping the time of day), so "
              "'created_at (as stored)' is not the real upload date; 'updated_at' is the last change. A run's calls are "
              "matched by time of day inside the run's window, the same way as verify_run.py."]
    return "\n".join(lines) + "\n", outcome, pipe, unfinished


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", type=Path, nargs="*", help="run result files to break down call by call")
    ap.add_argument("--env-file", type=Path, default=HERE / "qat" / "perf.env")
    ap.add_argument("--tz-offset-min", type=int, default=0, help="minutes to add if QAT returns times without a zone")
    ap.add_argument("--max-pages", type=int, default=600, help=f"safety cap on list pages of {V.PAGE_SIZE} calls")
    args = ap.parse_args()

    cfg = load_env_file(args.env_file)
    for key in ("BASE_URL", "PERF_EMAIL", "GROUP_ID"):
        if not cfg.get(key):
            print(f"{key} is required in {args.env_file}", file=sys.stderr)
            return 2
    base = cfg["BASE_URL"].rstrip("/")
    results = []
    for p in args.results:  # validated before login, so a typo doesn't waste an OTP
        if p.name.endswith(("_k6.json", "_verify.json")):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            print(f"Cannot read {p}: {err}", file=sys.stderr)
            return 2
        if r.get("t0_epoch") and r.get("group_id") == cfg["GROUP_ID"]:
            r["_path"] = str(p)
            results.append(r)

    try:
        token, refresh = login(base, cfg["PERF_EMAIL"], cfg.get("FORCE_LOGIN", "").lower() == "true")
        tracker = Tracker(base, token, refresh, cfg["GROUP_ID"])
        print(f"Reading every call in group {cfg['GROUP_ID']} ...")
        items, pages, complete = V.fetch_group_calls(tracker, cfg["GROUP_ID"], args.max_pages)
    except (ApiError, KeyboardInterrupt) as err:
        print(f"Audit stopped: {err or 'interrupted'}", file=sys.stderr)
        return 1
    items = list({it.get("id") or i: it for i, it in enumerate(items)}.values())  # pages can shift while reading

    runs = per_run(results, items, args.tz_offset_min)
    md, outcome, pipe, unfinished = render(items, complete, runs, base, cfg["GROUP_ID"])
    out = HERE / cfg.get("RESULTS_DIR", "qat/results") / f"group_audit_{datetime.now():%Y%m%d_%H%M%S}"
    try:
        Path(f"{out}.md").write_text(md, encoding="utf-8")
        Path(f"{out}.json").write_text(json.dumps({
            "group_id": cfg["GROUP_ID"], "calls": len(items), "complete": complete, "outcome": dict(outcome),
            "unfinished_pipeline_status": dict(pipe),
            "runs": [{"run": lbl, "uploaded": up, "outcome": dict(c), "result": path} for lbl, up, c, path in runs],
            "unfinished": [{k: it.get(k) for k in ("id", "display_id", "call_id", "qa_status", "pipeline_overall_status",
                                                   "review_state", "created_at", "updated_at")} for it in unfinished],
        }, indent=2), encoding="utf-8")
    except OSError as err:
        print(f"Cannot write the audit: {err}", file=sys.stderr)
        return 1
    print("\n" + md.split("## Unfinished calls")[0])
    print(f"Full list of {len(unfinished):,} unfinished calls: {out}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

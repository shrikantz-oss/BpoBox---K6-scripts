"""One-page summary of a burst sequence: a verdict per burst size, the typical value and range over
repeated runs, the per-call verification result, and a comparison with earlier runs of the same setup.

Written automatically by run_burst.py at the end of a sequence (burst_summary_<ts>.md); rebuild with:
  python -I burst_summary.py qat/results/e2e_burst*_20261104_*.json -o qat/results/burst_summary.md
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import burst_report as B  # noqa: E402

DONE = "all uploaded calls processed"
SITE_TARGET_MS = 2000  # 95% of page clicks answered within 2 s


VERIFIED = {"match", "time_corrected"}  # time_corrected: every call there and finished, report's time replaced by QAT's


def numbers(r):
    """burst_numbers, with QAT's per-call times replacing the report's when the run was verified."""
    n = B.burst_numbers(r)
    v = r.get("verify") or {}
    n["qat_time"] = False
    if v.get("verdict") in VERIFIED and v.get("qat_last_finished_s") is not None:
        n["total_s"] = v["qat_last_finished_s"] - n["up_start"]
        if v.get("qat_last_created_s") is not None:
            n["created_s"] = v["qat_last_created_s"] - n["up_start"]
        n["qat_time"] = True
    return n


def _key(r):
    b = (r.get("plan") or {}).get("burst") or {}
    return b.get("calls"), int(r.get("calls_per_zip") or 25), int(b.get("stagger_s") or 0)


def _med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _span(vals, fmt):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return ""
    return f"{fmt(min(vals))} – {fmt(max(vals))}"


def load_history(results_dir: Path, exclude_run_ids):
    """Completed burst runs already in results_dir, outside this sequence."""
    out = []
    for p in sorted(results_dir.glob("e2e_burst*.json")):
        if p.name.endswith(("_k6.json", "_verify.json")):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("end_reason") == DONE and r.get("run_id") not in exclude_run_ids and (r.get("plan") or {}).get("burst"):
            out.append(r)
    return out


def summarize(results, history=()):
    """Markdown summary for this sequence's results (list of run result dicts)."""
    groups, hist = defaultdict(list), defaultdict(list)
    for r in results:
        groups[_key(r)].append(r)
    for r in history:
        hist[_key(r)].append(r)

    rows, verdicts = [], []
    for key in sorted(groups, key=lambda k: (k[0] or 0, k[1], k[2])):
        size, cpz, stagger = key
        rs = groups[key]
        nums = [numbers(r) for r in rs]
        done = [n for n, r in zip(nums, rs) if r.get("end_reason") == DONE]
        ver = [(r.get("verify") or {}).get("verdict") for r in rs]
        upload_ok = min((n["upload_ok"] if n["upload_ok"] is not None else 1) for n in nums)
        site = _med([n["site_p95"] for n in done])
        total = _med([n["total_s"] for n in done])
        prev = _med([numbers(r)["total_s"] for r in hist.get(key, [])])

        if len(done) < len(rs) or upload_ok < 1 or "mismatch" in ver:
            verdict = "❌ FAIL"
        elif site is not None and site > SITE_TARGET_MS:
            verdict = "⚠️ PASS · website slow"
        else:
            verdict = "✅ PASS"
        if verdict != "❌ FAIL" and not all(v in VERIFIED for v in ver):
            verdict += " · not verified"
        corrected = any(n["qat_time"] for n in done) and "time_corrected" in ver
        verdicts.append(verdict)

        change = ""
        if prev and total:
            pct = (total - prev) / prev * 100
            change = f"{B.mins(prev)} ({'+' if pct >= 0 else ''}{pct:.0f}%)"
        label = f"{size:,} calls · {-(-size // cpz)}×{cpz}" + (f" · staggered {stagger} s" if stagger else "")
        rows.append([
            label, f"{len(done)}/{len(rs)}",
            f"{sum(v in VERIFIED for v in ver)}/{len(rs)}" if any(ver) else "skipped",
            (B.mins(total) + (" ¹" if corrected else "")) if total is not None else "–",
            _span([n["total_s"] for n in done], B.mins),
            B.mins(_med([n["created_s"] for n in done])) if done else "–",
            f"{site / 1000:.1f} s" if site is not None else "–",
            f"{upload_ok * 100:.0f}%", change or "no earlier run", verdict,
        ])

    head = ["Burst", "Completed", "Verified per call", "Typical time", "Range", "All created",
            "Website 95%", "Uploads OK", "Earlier typical (change)", "Verdict"]
    lines = ["# Burst sequence summary", ""]
    s = [r.get("setup") or {} for r in results]
    if s and s[0]:
        lines.append(f"- Setup: commit `{s[0].get('git_commit')}` · {s[0].get('k6_version', '').split(' (')[0]} · "
                     f"AI {s[0].get('ai_mode')} · polled every {s[0].get('poll_interval_s')} s · "
                     f"{s[0].get('browse_users')} simulated website users · load from `{s[0].get('load_generator')}`")
    starts = sorted(r.get("started_utc", "") for r in results)
    if starts:
        lines.append(f"- Runs: {len(results)} between {starts[0]} and {starts[-1]} (UTC)")
    if any(k[2] for k in groups):
        lines.append("- ⚠️ Staggered bursts start zips some seconds apart instead of all at once. That is gentler on the system, "
                     "so their times are NOT comparable with standard bursts and are kept separate in every report.")
    lines += ["", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    if any("¹" in str(row[3]) for row in rows):
        lines += ["", "¹ Time taken from QAT's own per-call records. The live progress count finished earlier than the real "
                  "last call because leftover calls from earlier runs completed during the burst and filled the group total."]
    lines += ["", "**How to read it**",
              "- *Typical time* is the median from sending the burst until its last call was analysed or skipped; *Range* shows "
              "best–worst when a size ran more than once.",
              "- *Verified per call* lists the run's own calls in QAT's call list and checks every one was created and "
              "finished; a mismatch means the report and QAT disagree.",
              "- *Earlier typical* for runs before 4 Nov uses the live progress count (no per-call check existed yet), "
              "so it can be slightly optimistic.",
              f"- *Website 95%* is how long 95 of 100 page clicks took during the burst (target {SITE_TARGET_MS / 1000:.0f} s).",
              "- *Earlier typical* is the median of all earlier completed runs with the same size, zip packing and upload pattern."]
    overall = "❌ FAIL" if any(v.startswith("❌") for v in verdicts) else (
        "⚠️ PASS with warnings" if any(v.startswith("⚠️") or "not verified" in v for v in verdicts) else "✅ PASS")
    return "\n".join(lines[:2] + [f"**Overall: {overall}**", ""] + lines[2:]) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="write the markdown here as well as printing it")
    args = ap.parse_args()
    results = []
    for p in args.results:
        if p.name.endswith(("_k6.json", "_verify.json")):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            print(f"Cannot read {p}: {err}", file=sys.stderr)
            return 1
        if (r.get("plan") or {}).get("burst"):
            results.append(r)
    if not results:
        print("No burst results given.", file=sys.stderr)
        return 1
    md = summarize(results, load_history(args.results[0].parent, {r.get("run_id") for r in results}))
    print(md)
    if args.out:
        try:
            args.out.write_text(md, encoding="utf-8")
        except OSError as err:
            print(f"Cannot write {args.out}: {err}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

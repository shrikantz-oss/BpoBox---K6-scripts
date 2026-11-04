"""Burst test: send N calls at once, time how long until every call is fully processed.

Default sequence 100 → 200 → 500 → 1000 → 2000 calls. Each burst is uploaded as
ceil(N / CALLS_PER_ZIP) zips all at the same moment, then tracked until every call is
scored or skipped. The next burst starts only once the previous one is done (and any
leftover backlog in the group has cleared). One login (email OTP) for the whole sequence.

Writes per burst qat/results/e2e_burst<N>_<ts>.json (+ .html technical report) and, at the end,
qat/results/burst_report_<ts>.html: the plain-language report with analogies and a glossary.

Usage:
  python -I run_burst.py                       # 100,200,500,1000,2000
  python -I run_burst.py --sizes 100,500 --yes
  python -I burst_report.py qat/results/e2e_burst*_2026*.json -o qat/results/burst_report.html   # rebuild later
"""
import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import burst_report  # noqa: E402
from run_e2e import AUDIO_EXTS, Tracker, burst_spec, run_one  # noqa: E402
from run_perf import ApiError, load_env_file, login, validate_ids  # noqa: E402


def _cooldown(seconds: int) -> bool:
    """Sleep between bursts. A single Ctrl+C (e.g. pressed to copy text) doesn't end the
    sequence: it takes a second Ctrl+C within 10 s. Returns False if the user confirmed stop."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            time.sleep(min(1, end - time.time()))
        except KeyboardInterrupt:
            print("\nCtrl+C received. Press Ctrl+C again within 10 s to stop the sequence; otherwise it continues.")
            try:
                time.sleep(10)
            except KeyboardInterrupt:
                return False
            print("Continuing.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="100,200,500,1000,2000", help="comma-separated burst sizes (calls)")
    ap.add_argument("--env-file", type=Path, default=HERE / "qat" / "perf.env")
    ap.add_argument("--calls-per-zip", type=int, help="override CALLS_PER_ZIP, e.g. 10 for a 10-call dry run in one zip")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    try:
        sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    except ValueError:
        print("--sizes must be comma-separated whole numbers", file=sys.stderr)
        return 2
    if not sizes or any(n < 1 or n > 20000 for n in sizes):
        print("Burst sizes must be 1..20000", file=sys.stderr)
        return 2

    cfg = load_env_file(args.env_file)
    for k in ("BASE_URL", "PERF_EMAIL", "GROUP_ID", "SOURCE_DIR"):
        if not cfg.get(k):
            print(f"{k} is required in {args.env_file}", file=sys.stderr)
            return 2
    base = cfg["BASE_URL"].rstrip("/")
    cpz = args.calls_per_zip or int(cfg.get("CALLS_PER_ZIP", "25"))
    if not 1 <= cpz <= 500:
        print("Calls per zip must be 1..500", file=sys.stderr)
        return 2
    ctx = {
        "cfg": {**cfg, "BROWSE_VUS": cfg.get("BROWSE_VUS", "5")}, "base": base, "peak": 0, "soak_min": 0,
        "calls_per_zip": cpz,
        "poll_s": int(cfg.get("BURST_POLL_INTERVAL_S", "15")),   # finer timing than the paced scenarios
        "stall_min": int(cfg.get("STALL_TIMEOUT_MIN", "20")),
        "max_drain_min": int(cfg.get("MAX_DRAIN_MIN", "180")),
        "results_dir": HERE / cfg.get("RESULTS_DIR", "qat/results"),
        "predrain_max_min": int(cfg.get("PREDRAIN_MAX_MIN", "45")),
        "stuck_ok": int(cfg.get("STUCK_OK", "20")),
    }
    ctx["results_dir"].mkdir(parents=True, exist_ok=True)

    files = [p for p in Path(cfg["SOURCE_DIR"]).rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    if not files:
        print(f"No .mp3/.wav/.m4a files in {cfg['SOURCE_DIR']}", file=sys.stderr)
        return 2
    avg_mb = sum(p.stat().st_size for p in files) / len(files) / 1024 / 1024
    print("\nBurst sequence (each burst sent all at once, next one starts after the previous is fully processed):")
    for n in sizes:
        zips = -(-n // cpz)
        print(f"  {n:>6,} calls  = {zips:>4} zips of {cpz} uploaded in parallel  (~{zips * cpz * avg_mb:,.0f} MB)")
    total = sum(-(-n // cpz) * cpz for n in sizes)
    print(f"Total      : {total:,} calls, ~{total * avg_mb:,.0f} MB")
    print(f"Target     : {base}  group {cfg['GROUP_ID']}")
    print("Cost       : AI is free only if the Lambdas run with MOCK_AI=true; AWS usage is billed as normal.\n")
    if not args.yes and input("Type 'burst' to start, anything else to cancel: ").strip() != "burst":
        print("Cancelled.")
        return 1

    try:
        token, refresh = login(base, cfg["PERF_EMAIL"], cfg.get("FORCE_LOGIN", "true").lower() == "true")
        validate_ids(base, token, cfg)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    ctx["k6"] = shutil.which("k6")
    if not ctx["k6"]:
        print("k6 not found on PATH", file=sys.stderr)
        return 2
    ctx["tracker"] = Tracker(base, token, refresh, cfg["GROUP_ID"])

    results = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for i, n in enumerate(sizes):
        if i:
            ctx["tracker"]._refresh()  # fresh 60-min token for the next k6 run
            print("\nCooling down 60 s before the next burst...")
            if not _cooldown(60):
                print("Stopped before the next burst.")
                break
        res, interrupted = run_one(f"burst{n}", ctx, burst_spec(n, cpz))
        if res:
            results.append(res)
            # Rebuild the friendly report after every burst, so a stopped sequence still has one.
            out = ctx["results_dir"] / f"burst_report_{stamp}.html"
            out.write_text(burst_report.render(results), encoding="utf-8")
            print(f"Burst report updated: {out}")
        if interrupted or res is None:
            print("Sequence stopped; remaining bursts skipped.")
            break

    ok = len(results) == len(sizes) and all(r["end_reason"] == "all uploaded calls processed" for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

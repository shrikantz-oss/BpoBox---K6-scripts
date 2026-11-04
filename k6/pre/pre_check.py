"""Is PRE analysing calls on the Lambda pipeline? Reads the newest smoke result in the PRE results folder.

PRE picks its pipeline from the server's DIALPAD_PIPELINE setting (default "celery"); the test cannot change it.
Lambda analysed 27-47 calls/min on QAT, Celery about 1/min, so the analysis rate of a 100-call smoke run tells
them apart. Exit code 0 = Lambda speed, 1 = too slow (likely Celery) or failed, 2 = no smoke result yet.

  python -I pre\\pre_check.py [--env-file pre\\perf.env]
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # perf/k6/pre: PRE config and results
K6 = HERE.parent  # perf/k6: the shared test scripts
sys.path.insert(0, str(K6))  # `python -I` drops the script dir from sys.path
import e2e_report as R  # noqa: E402
from run_perf import load_env_file  # noqa: E402

DONE = "all uploaded calls processed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=HERE / "perf.env")
    args = ap.parse_args()
    cfg = load_env_file(args.env_file)
    min_rate = float(cfg.get("LAMBDA_MIN_RATE", "10"))
    res = K6 / cfg.get("RESULTS_DIR", "pre/results")  # resolved like run_e2e.py does: relative to perf/k6
    files = sorted(p for p in res.glob("e2e_smoke_*.json") if not p.name.endswith("_k6.json"))
    if not files:
        print(f"No smoke result in {res}. Run the check first: .\\pre\\run_pre.ps1 check")
        return 2
    try:
        r = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Cannot read {files[-1].name}: {err}")
        return 1

    tl = r.get("timeline") or []
    sent = r.get("uploaded_calls") or 0
    if not sent or not tl:
        print(f"{files[-1].name}: no calls were uploaded or tracked ({r.get('end_reason')}).")
        return 1
    t_done = R.derive(r)["t_all_processed"]
    if r.get("end_reason") == DONE and t_done:
        rate, how = sent / (t_done / 60), f"all {sent} calls processed in {t_done / 60:.1f} min"
    else:  # stopped early (e.g. Ctrl+C on a slow run): judge on what was processed so far
        last = tl[-1]
        rate = last["processed"] / (last["t"] / 60) if last["t"] else 0
        how = f"{last['processed']} of {sent} processed after {last['t'] / 60:.1f} min ({r.get('end_reason')})"

    print(f"{files[-1].name}: {how} -> {rate:.1f} calls/min analysed")
    if rate < min_rate:
        print(f"TOO SLOW for Lambda (needs >= {min_rate:g}/min). PRE is probably on the Celery pipeline: ask DevOps to set "
              "DIALPAD_PIPELINE=lambda on PRE (and check the PRE Lambdas/SQS are deployed), then run the check again.")
        return 1
    print("OK: PRE is analysing at Lambda speed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

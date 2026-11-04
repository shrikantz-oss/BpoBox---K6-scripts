"""End-to-end scenario runner: smoke / load / peak / soak / spike.

For one scenario it:
  1. logs in (email OTP, + MFA) and validates the IDs  — reuses run_perf.py
  2. shows the plan (calls, data volume, duration) and asks you to confirm
  3. runs e2e_scenarios.js (k6) to upload calls as many small zips at the planned rate
  4. polls the pipeline the whole time (calls created / scored / skipped) and keeps
     polling after the upload phase until every uploaded call is processed
  5. writes qat/results/e2e_<scenario>_<ts>.json and an HTML report next to it

Usage:
  python -I run_e2e.py smoke                 # 100-call sanity run first
  python -I run_e2e.py load|peak|soak|spike
  python -I run_e2e.py peak --env-file pre.env --peak 500 --yes
  python -I e2e_report.py qat/results/e2e_*.json -o qat/results/e2e_combined.html   # one report for several runs
"""
import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # `python -I` drops the script dir from sys.path
import e2e_report  # noqa: E402
from run_perf import ApiError, load_env_file, login, validate_ids  # noqa: E402

AUDIO_EXTS = {".mp3", ".wav", ".m4a"}


STEP_MIN = 10  # minutes per step in the "step" scenario


# ─── Scenarios (calls/min). Everything scales from PEAK_CALLS_PER_MIN. ─────────
def scenarios(peak: int, soak_minutes: int, per_hour: int = 40000, target_minutes: int = 60) -> dict:
    load_r, soak_r, base_r = round(peak * 0.6), round(peak * 0.5), round(peak * 0.2)
    hour_r = per_hour / 60
    # Step test: hold each rate STEP_MIN minutes, climbing to the hourly target (10 s jumps between steps).
    step_rates = [200, 300, 450, round(hour_r)]
    step_stages = [st for r in step_rates for st in ((1 / 6, r), (STEP_MIN - 1 / 6, r))]
    return {
        "step": {"desc": "Step-up to " + f"{per_hour:,}/hour: " + " -> ".join(f"{r}/min" for r in step_rates)
                         + f", {STEP_MIN} min each; stops at the first failing step",
                 "start": 0, "stages": step_stages, "unit": 60},
        # Constant arrival at a calls-per-HOUR target; "unit": 60 makes k6 pace zips per hour, so e.g.
        # 40,000/h in zips of 100 is exactly 400 zips/h instead of rounding to 7 zips/min (42,000).
        "target": {"desc": f"Target throughput: {per_hour:,} calls/hour ({hour_r:,.0f}/min) for {target_minutes} min",
                   "start": hour_r, "stages": [(target_minutes, hour_r)], "unit": 60},
        "smoke": {"desc": "Sanity check: 50 calls/min for 2 min", "start": 50,
                  "stages": [(2, 50), (0.5, 0)]},
        "load": {"desc": f"Normal expected load: ramp to {load_r}/min (60% of peak), hold 15 min", "start": 0,
                 "stages": [(2, load_r), (15, load_r), (1, 0)]},
        "peak": {"desc": f"Maximum expected load: ramp to {peak}/min, hold 10 min", "start": 0,
                 "stages": [(3, peak), (10, peak), (1, 0)]},
        "soak": {"desc": f"Endurance: {soak_r}/min (50% of peak) for {soak_minutes} min", "start": 0,
                 "stages": [(3, soak_r), (soak_minutes, soak_r), (1, 0)]},
        "spike": {"desc": f"Bursts: {base_r}/min baseline, jumps to {peak}/min in 30 s, twice", "start": base_r,
                  "stages": [(3, base_r), (0.5, peak), (3, peak), (0.5, base_r), (5, base_r),
                             (0.5, peak), (3, peak), (0.5, base_r), (5, base_r)]},
    }


def planned_curve(start: float, stages, calls_per_zip: int, step_s: int = 15, unit: int = 1):
    """[(t_seconds, cumulative_planned_calls)] for a piecewise-linear arrival rate.

    Rates are rounded to whole zips/min exactly as e2e_scenarios.js does, and the total
    is whole zips, so "planned" matches what k6 can actually send.
    """
    as_zips = lambda cpm: round(cpm * unit / calls_per_zip) * calls_per_zip / unit  # noqa: E731  (whole zips per `unit` minutes)
    stages = [(m, as_zips(r)) for m, r in stages]
    pts, t, total, rate = [(0, 0.0)], 0.0, 0.0, float(as_zips(start))
    for minutes, target in stages:
        dur = minutes * 60
        steps = max(1, int(dur // step_s))
        for i in range(1, steps + 1):
            dt = dur / steps
            r0 = rate + (target - rate) * (i - 1) / steps
            r1 = rate + (target - rate) * i / steps
            total += (r0 + r1) / 2 * dt / 60
            t += dt
            pts.append((round(t), round(total, 1)))
        rate = target
    whole = int(total // calls_per_zip) * calls_per_zip
    pts[-1] = (pts[-1][0], float(whole))
    return pts


# ─── Source manifest (CRC32 precomputed so k6 can build zips without hashing) ──
def build_manifest(source_dir: Path, out: Path) -> list:
    files = sorted(p for p in source_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if not files:
        raise SystemExit(f"No .mp3/.wav/.m4a files in {source_dir}")
    manifest = [{"path": p.resolve().as_posix(), "size": p.stat().st_size,
                 "crc32": zlib.crc32(p.read_bytes()) & 0xFFFFFFFF} for p in files]
    out.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


# ─── Pipeline tracker (own token handling; runs alongside k6) ────────────────
class Tracker:
    def __init__(self, base, token, refresh, group_id):
        self.base, self.token, self.refresh_token, self.group = base, token, refresh, group_id
        self.token_at = time.time()
        self.lock = threading.Lock()

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        req = urllib.request.Request(f"{self.base}/auth/refresh", method="POST", data=b"")
        req.add_header("Cookie", f"refresh_token={self.refresh_token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read() or b"{}")
                for c in r.headers.get_all("Set-Cookie") or []:
                    if c.startswith("refresh_token="):
                        self.refresh_token = c.split(";", 1)[0].split("=", 1)[1]
            self.token = (body.get("data") or {}).get("access_token") or self.token
            self.token_at = time.time()
            return True
        except (OSError, http.client.HTTPException, ValueError) as e:  # URLError/timeouts/resets are OSError
            print(f"[tracker] token refresh failed: {e}", file=sys.stderr)
            return False

    def _get(self, path):
        if time.time() - self.token_at > 45 * 60:
            self._refresh()
        for attempt in (1, 2):
            req = urllib.request.Request(f"{self.base}{path}")
            req.add_header("Authorization", f"Bearer {self.token}")
            try:
                # 30 s: a hung database must fail fast so the safety stop can react
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1 and self._refresh():
                    continue
                raise ApiError(f"GET {path.split('?')[0]}: HTTP {e.code}")
            except (OSError, http.client.HTTPException, ValueError) as e:  # resets, disconnects, bad JSON
                raise ApiError(f"GET {path.split('?')[0]}: {e}")
        raise ApiError("unreachable")

    def counts(self) -> dict:
        """Absolute counts in the group: total, scored (evaluated), skipped (sampling/too short)."""
        q = f"/qa/calls?group_ids={self.group}&page=1&page_size=1"
        with self.lock:
            all_ = self._get(q)
            ev = self._get(q + "&tab=evaluated_calls")
        d = all_.get("data") or {}
        c = (d.get("meta") or {}).get("counts") or {}
        return {
            "total": (d.get("pagination") or {}).get("total_items", 0),
            "scored": ((ev.get("data") or {}).get("pagination") or {}).get("total_items", 0),
            "skipped": (c.get("not_reviewed") or 0) + (c.get("not_eligible") or 0),
        }


ALL_ORDER = ["load", "peak", "spike", "soak"]  # soak last: longest


K6_API_DEFAULT = "127.0.0.1:6566"  # k6 REST API for stopping a burst early (6565 is k6's own default)


def _k6_stop(address: str) -> bool:
    """Ask the running k6 to end the test gracefully (handleSummary still runs)."""
    body = json.dumps({"data": {"type": "status", "id": "default", "attributes": {"stopped": True}}}).encode()
    req = urllib.request.Request(f"http://{address}/v1/status", data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 300
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Could not stop k6 via its API ({e}); it will end after BURST_BROWSE_S.", file=sys.stderr)
        return False


def _k6_metric(address: str, metric: str, field: str = "count") -> float:
    """Live value of a k6 metric from its REST API (0 if not reported yet)."""
    try:
        with urllib.request.urlopen(f"http://{address}/v1/metrics/{metric}", timeout=5) as r:
            return float(json.load(r)["data"]["attributes"]["sample"].get(field, 0))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return 0.0


def _watch_steps(proc, plan, timeline, consec_fail, server_down_after, cpz, cfg, k6_addr, t0):
    """Step scenario: stop k6 at the first failing step. Returns {step, rate, reason, t} or None.

    A step fails when uploads are rejected (STEP_FAIL_ZIPS failed zips) or the API falls more than
    STEP_MAX_BACKLOG_ZIPS zips behind in unpacking (beyond that the DB pool runs out, as in the 40-zip burst).
    """
    fail_zips = int(cfg.get("STEP_FAIL_ZIPS", "2"))
    max_backlog = int(cfg.get("STEP_MAX_BACKLOG_ZIPS", "10")) * cpz
    bounds, acc = [], 0.0  # (end_s, calls/min) per hold stage
    for st in plan["stages"]:
        acc += st["minutes"] * 60
        bounds.append((acc, st["callsPerMin"]))
    step_rates = list(dict.fromkeys(r for _, r in bounds))
    last_step = None
    while proc.poll() is None:
        t = time.time() - t0
        rate = next((r for end, r in bounds if t < end), bounds[-1][1])
        step = step_rates.index(rate) + 1
        if step != last_step:
            print(f"[step] >>> Step {step}/{len(step_rates)}: {rate}/min ({rate * 60:,}/hour) from +{int(t) // 60}m")
            last_step = step
        reason = None
        failed = _k6_metric(k6_addr, "zips_failed")
        uploaded = _k6_metric(k6_addr, "calls_uploaded")
        s = timeline[-1] if timeline else None
        if failed >= fail_zips:
            reason = f"{int(failed)} zip uploads rejected by the API"
        elif s and uploaded - s["ingested"] > max_backlog:
            reason = (f"API {int(uploaded - s['ingested']):,} calls behind in unpacking "
                      f"(> {max_backlog // cpz} zips; DB pool would run out)")
        elif consec_fail[0] >= server_down_after:
            reason = f"server not responding ({consec_fail[0]} failed checks in a row)"
        if reason:
            print(f"[step] XXX STEP {step} FAILED at {rate}/min: {reason} - stopping uploads.")
            _k6_stop(k6_addr)
            return {"step": step, "rate": rate, "reason": reason, "t": round(t),
                    "passed_rate": step_rates[step - 2] if step > 1 else None}
        time.sleep(10)
    print("[step] All steps completed without a failure.")
    return None


def burst_spec(calls: int, calls_per_zip: int) -> dict:
    """All `calls` sent at once as ceil(calls/calls_per_zip) zips uploaded in parallel."""
    zips = -(-calls // calls_per_zip)
    return {"desc": f"Burst: {calls:,} calls sent at once ({zips} zips uploaded in parallel)",
            "plan": {"name": f"burst{calls}", "startCallsPerMin": 0, "stages": [], "burst": {"zips": zips, "calls": calls}},
            "curve": [(0, 0.0), (1, float(zips * calls_per_zip))], "planned_calls": zips * calls_per_zip}


def run_one(name, ctx, spec=None) -> tuple:
    """Run one scenario (or a burst `spec`) end to end. Returns (result dict | None, interrupted)."""
    cfg, tracker, results_dir = ctx["cfg"], ctx["tracker"], ctx["results_dir"]
    cpz = ctx["calls_per_zip"]
    if spec is None:
        sc = scenarios(ctx["peak"], ctx["soak_min"], ctx["per_hour"], ctx["target_min"])[name]
        curve = planned_curve(sc["start"], sc["stages"], cpz, unit=sc.get("unit", 1))
        plan = {"name": name, "startCallsPerMin": sc["start"], "unitMinutes": sc.get("unit", 1),
                "stages": [{"minutes": m, "callsPerMin": r} for m, r in sc["stages"]]}
    else:
        sc, curve, plan = {"desc": spec["desc"]}, spec["curve"], spec["plan"]
    planned_calls = int(curve[-1][1])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"e2e{name[:2]}{datetime.now().strftime('%m%d%H%M%S')}"
    build_manifest(Path(cfg["SOURCE_DIR"]), results_dir / f"manifest_{run_id}.json")

    # Calls left over from an earlier (stopped) run would count as this run's progress.
    # Wait until the group's backlog stops shrinking; what remains is permanently stuck.
    try:
        prev, deadline = None, time.time() + ctx["predrain_max_min"] * 60
        while True:
            c = tracker.counts()
            backlog = c["total"] - c["scored"] - c["skipped"]
            if prev is not None and backlog >= prev:
                break
            if time.time() > deadline:
                print(f"[{name}] Pre-run backlog still {backlog} after {ctx['predrain_max_min']} min; starting anyway.")
                break
            if prev is None and backlog <= ctx["stuck_ok"]:
                break
            print(f"[{name}] Waiting for {backlog:,} leftover calls in the group to finish before starting...")
            prev = backlog
            time.sleep(60)
        baseline = tracker.counts()
    except (ApiError, KeyboardInterrupt) as e:
        print(f"ERROR before start: {e}", file=sys.stderr)
        return None, isinstance(e, KeyboardInterrupt)
    print(f"\n=== {name}: {sc['desc']} - ~{planned_calls:,} calls ===")
    print(f"Baseline in group: {baseline['total']} calls, {baseline['scored']} scored, {baseline['skipped']} skipped\n")

    timeline, poll_errors = [], [0]
    t0 = time.time()
    stop_polling = threading.Event()

    consec_fail = [0]  # consecutive failed polls: the server-down signal
    server_down_after = int(cfg.get("SERVER_DOWN_POLLS", "4"))  # 4 × poll interval (~1 min for bursts)

    def sample():
        try:
            c = tracker.counts()
        except Exception as e:  # noqa: BLE001 - the poller must survive; failures feed the safety stop
            poll_errors[0] += 1
            consec_fail[0] += 1
            print(f"[tracker] poll failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None
        consec_fail[0] = 0
        s = {"t": round(time.time() - t0),
             "ingested": max(0, c["total"] - baseline["total"]),
             "scored": max(0, c["scored"] - baseline["scored"]),
             "skipped": max(0, c["skipped"] - baseline["skipped"])}
        s["processed"] = s["scored"] + s["skipped"]
        timeline.append(s)
        return s

    def poll_loop():
        while not stop_polling.wait(ctx["poll_s"]):
            s = sample()
            if s:
                print(f"[{name} +{s['t'] // 60:>3}m{s['t'] % 60:02d}s] created {s['ingested']:>6}  scored {s['scored']:>6}  "
                      f"skipped {s['skipped']:>5}  backlog {s['ingested'] - s['processed']:>6}")

    summary_path = results_dir / f"e2e_{name}_{stamp}_k6.json"
    # Hand k6 the tracker's current tokens (refreshed between scenarios in a sequence).
    child_env = {**os.environ, **{k: v for k, v in cfg.items() if v and k not in ("PERF_EMAIL", "FORCE_LOGIN")},
                 "ACCESS_TOKEN": tracker.token, "REFRESH_TOKEN": tracker.refresh_token or "", "RUN_ID": run_id,
                 "CALLS_PER_ZIP": str(cpz), "SCENARIO_JSON": json.dumps(plan),
                 "SOURCE_MANIFEST": (results_dir / f"manifest_{run_id}.json").as_posix(),
                 "K6_SUMMARY_PATH": summary_path.as_posix()}
    if spec is not None:
        # Browsing lasts until the burst is processed (we stop k6 then); this is just a safety cap.
        child_env["BURST_BROWSE_S"] = cfg.get("BURST_BROWSE_S", "7200")

    interrupted, server_down = False, False
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    k6_started = datetime.now(timezone.utc)
    # Per-sample CSV feeds the time-series graphs (response time, throughput, errors, VUs).
    csv_path = results_dir / f"e2e_{name}_{stamp}_k6.csv.gz"
    k6_cmd = [ctx["k6"], "run", "--quiet", "--out", f"csv={csv_path.as_posix()}"]
    k6_addr = cfg.get("K6_API_ADDRESS", K6_API_DEFAULT)
    if spec is not None or name == "step":
        # Burst / step: k6 is stopped through its REST API (graceful: the summary is still written).
        k6_cmd += ["--address", k6_addr]
    proc = subprocess.Popen(k6_cmd + ["e2e_scenarios.js"], cwd=HERE, env=child_env)
    step_stop = None
    try:
        if name == "step":
            step_stop = _watch_steps(proc, plan, timeline, consec_fail, server_down_after, cpz, cfg, k6_addr, t0)
            server_down = bool(step_stop and step_stop["reason"].startswith("server"))
        elif spec is not None:
            stop_sent = False
            while proc.poll() is None:
                s = timeline[-1] if timeline else None
                if (not stop_sent and s and s["ingested"] >= planned_calls
                        and s["processed"] >= planned_calls):
                    print(f"[{name}] All {planned_calls:,} calls processed - stopping the browsing users.")
                    stop_sent = _k6_stop(cfg.get("K6_API_ADDRESS", K6_API_DEFAULT))
                if not stop_sent and consec_fail[0] >= server_down_after:
                    # Safety stop: the server stopped answering, so stop sending load at once.
                    print(f"[{name}] SERVER NOT RESPONDING ({consec_fail[0]} failed checks in a row) - "
                          "stopping the test to avoid adding load.")
                    stop_sent = _k6_stop(cfg.get("K6_API_ADDRESS", K6_API_DEFAULT))
                    server_down = True
                time.sleep(2)
        rc = proc.wait()
    except KeyboardInterrupt:
        # k6 got the same Ctrl+C: let it finish writing its summary instead of killing it.
        interrupted = True
        try:
            rc = proc.wait(timeout=120)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            rc = 130
    upload_end_s = round(time.time() - t0)

    k6_summary = {}
    if summary_path.is_file():
        k6_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    uploaded = int((((k6_summary.get("metrics") or {}).get("calls_uploaded") or {}).get("values") or {}).get("count", 0))
    print(f"\n[{name}] Upload phase finished (k6 exit {rc}). Calls uploaded: {uploaded:,}. Tracking until all are processed "
          f"(stall {ctx['stall_min']} min, max {ctx['max_drain_min']} min). Ctrl+C to stop and write the report.\n")

    # ─── Drain: keep polling until every uploaded call is created and processed ──
    end_reason = "interrupted during upload" if interrupted else None
    if server_down:
        interrupted, end_reason = True, "server stopped responding (safety stop)"
    if not interrupted and uploaded:
        last_change, last_key = time.time(), None
        drain_deadline = time.time() + ctx["max_drain_min"] * 60
        try:
            while True:
                s = timeline[-1] if timeline else None
                if s and s["ingested"] >= uploaded and s["processed"] >= uploaded:
                    end_reason = "all uploaded calls processed"
                    break
                key = (s["ingested"], s["processed"]) if s else None
                if key != last_key:
                    last_key, last_change = key, time.time()
                if time.time() - last_change > ctx["stall_min"] * 60:
                    end_reason = f"no progress for {ctx['stall_min']} min"
                    break
                if time.time() > drain_deadline:
                    end_reason = f"max drain time ({ctx['max_drain_min']} min) reached"
                    break
                if consec_fail[0] >= server_down_after:
                    print(f"[{name}] SERVER NOT RESPONDING - stopping the sequence.")
                    interrupted, end_reason = True, "server stopped responding (safety stop)"
                    break
                time.sleep(5)
        except KeyboardInterrupt:
            interrupted = True
            end_reason = "stopped by user during drain"
    stop_polling.set()
    final = sample() or (timeline[-1] if timeline else None)
    if not end_reason:
        end_reason = "no calls uploaded"

    result = {
        "scenario": name, "description": sc["desc"], "run_id": run_id,
        "started_utc": k6_started.isoformat(timespec="seconds"),
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": ctx["base"], "group_id": cfg["GROUP_ID"], "peak_calls_per_min": ctx["peak"],
        "calls_per_zip": cpz, "plan": plan, "planned_curve": curve,
        "planned_calls": planned_calls, "uploaded_calls": uploaded, "upload_end_s": upload_end_s,
        "baseline": baseline, "timeline": timeline, "final": final, "poll_errors": poll_errors[0],
        "end_reason": end_reason, "k6_exit_code": rc, "k6": e2e_report.k6_extract(k6_summary),
        "t0_epoch": t0, "k6_csv": csv_path.name, "step_stop": step_stop,
        "ai_mode": cfg.get("AI_MODE", "unknown").lower(),  # mock | real | unknown, for the reports' wording
    }
    if csv_path.is_file():
        try:
            result["k6_series"] = e2e_report.k6_timeseries(csv_path, t0)
            # In a burst, k6 keeps browsing after the last upload, so use the real upload window.
            if spec is not None and result["k6_series"].get("upload_last_end_s") is not None:
                result["upload_end_s"] = result["k6_series"]["upload_last_end_s"]
        except (OSError, ValueError) as err:
            print(f"Could not read k6 CSV for graphs: {err}", file=sys.stderr)
    out_json = results_dir / f"e2e_{name}_{stamp}.json"
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    out_html = out_json.with_suffix(".html")
    out_html.write_text(e2e_report.render([result]), encoding="utf-8")
    print(f"\n[{name}] End: {end_reason}\nResult : {out_json}\nReport : {out_html}")
    return result, interrupted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=["smoke", "load", "peak", "soak", "spike", "target", "step", "all"],
                    help="'all' = load, peak, spike, soak back to back with one login and a combined report")
    ap.add_argument("--env-file", type=Path, default=HERE / "qat" / "perf.env")
    ap.add_argument("--peak", type=int, help="peak calls/min (default PEAK_CALLS_PER_MIN or 500)")
    ap.add_argument("--soak-minutes", type=int, help="soak hold time (default SOAK_MINUTES or 60)")
    ap.add_argument("--per-hour", type=int, help="'target' scenario: calls per hour (default TARGET_CALLS_PER_HOUR or 40000)")
    ap.add_argument("--minutes", type=int, help="'target' scenario: how long to keep sending (default TARGET_MINUTES or 60)")
    ap.add_argument("--only", help="with 'all': comma-separated subset, e.g. load,peak")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    cfg = load_env_file(args.env_file)
    for k in ("BASE_URL", "PERF_EMAIL", "GROUP_ID", "SOURCE_DIR"):
        if not cfg.get(k):
            print(f"{k} is required in {args.env_file} (SOURCE_DIR = folder of test recordings)", file=sys.stderr)
            return 2
    base = cfg["BASE_URL"].rstrip("/")
    ctx = {
        "cfg": cfg, "base": base,
        "peak": args.peak or int(cfg.get("PEAK_CALLS_PER_MIN", "500")),
        "soak_min": args.soak_minutes or int(cfg.get("SOAK_MINUTES", "60")),
        "per_hour": args.per_hour or int(cfg.get("TARGET_CALLS_PER_HOUR", "40000")),
        "target_min": args.minutes or int(cfg.get("TARGET_MINUTES", "60")),
        "calls_per_zip": int(cfg.get("CALLS_PER_ZIP", "25")),
        "poll_s": int(cfg.get("POLL_INTERVAL_S", "30")),
        "stall_min": int(cfg.get("STALL_TIMEOUT_MIN", "20")),
        "max_drain_min": int(cfg.get("MAX_DRAIN_MIN", "180")),
        "results_dir": HERE / cfg.get("RESULTS_DIR", "qat/results"),
        "predrain_max_min": int(cfg.get("PREDRAIN_MAX_MIN", "45")),
        "stuck_ok": int(cfg.get("STUCK_OK", "20")),  # known permanently-stuck calls to ignore
    }
    if not 1 <= ctx["calls_per_zip"] <= 500:
        print("CALLS_PER_ZIP must be 1..500", file=sys.stderr)
        return 2
    ctx["results_dir"].mkdir(parents=True, exist_ok=True)
    names = [args.scenario]
    if args.scenario == "all":
        names = [n.strip() for n in args.only.split(",")] if args.only else ALL_ORDER
        bad = [n for n in names if n not in scenarios(1, 1)]
        if bad:
            print(f"Unknown scenario(s) in --only: {bad}", file=sys.stderr)
            return 2

    # Plan everything up front, then one confirmation.
    files = sorted(p for p in Path(cfg["SOURCE_DIR"]).rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if not files:
        print(f"No .mp3/.wav/.m4a files in {cfg['SOURCE_DIR']}", file=sys.stderr)
        return 2
    avg_kb = sum(p.stat().st_size for p in files) / len(files) / 1024
    total_calls, total_min = 0, 0.0
    print()
    for n in names:
        sc = scenarios(ctx["peak"], ctx["soak_min"], ctx["per_hour"], ctx["target_min"])[n]
        calls = int(planned_curve(sc["start"], sc["stages"], ctx["calls_per_zip"], unit=sc.get("unit", 1))[-1][1])
        mins = sum(m for m, _ in sc["stages"])
        total_calls, total_min = total_calls + calls, total_min + mins
        print(f"{n:6s}: {sc['desc']}")
        print(f"        start {sc['start']:,.0f}/min, then " + ", ".join(f"{m:g} min->{r:,.0f}/min" for m, r in sc["stages"])
              + f"  ->  ~{calls:,} calls, {mins:g} min of upload")
    print(f"\nTotal     : ~{total_calls:,} calls, ~{total_calls * avg_kb / 1024:,.0f} MB, {total_min:g} min of upload "
          f"+ drain after each scenario (max {ctx['max_drain_min']} min each)")
    print(f"Target    : {base}  group {cfg['GROUP_ID']}")
    print("Cost      : every eligible call is transcribed and LLM-scored (free only if the Lambdas run with MOCK_AI=true).\n")
    if not args.yes:
        if input(f"Type '{args.scenario}' to start, anything else to cancel: ").strip() != args.scenario:
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
    for i, n in enumerate(names):
        if i:
            ctx["tracker"]._refresh()  # fresh 60-min token for the next k6 run
            print("\nCooling down 60 s before the next scenario...")
            try:
                time.sleep(60)
            except KeyboardInterrupt:
                print("Stopped before the next scenario.")
                break
        res, interrupted = run_one(n, ctx)
        if res:
            results.append(res)
        if interrupted or res is None:
            if len(names) > 1:
                print("Sequence stopped; remaining scenarios skipped.")
            break

    if len(results) > 1:
        out = ctx["results_dir"] / f"e2e_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        out.write_text(e2e_report.render(results), encoding="utf-8")
        print(f"\nCombined report: {out}")
    ok = bool(results) and all(r["end_reason"] == "all uploaded calls processed" for r in results)
    return 0 if ok and len(results) == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())

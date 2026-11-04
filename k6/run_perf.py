"""Log in with email OTP (+MFA), validate the client/department/program/group IDs, then run k6.

The access token and refresh cookie are handed to k6 through the child process environment only:
never printed, never written to disk, never on the command line (so not in shell history / ps).

Usage:
  python -I run_perf.py                     # reads qat/perf.env (PRE: --env-file pre/perf.env)
  python -I run_perf.py --env-file qat/perf.env -- --out json=qat/results/raw.json   # extra args go to k6
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

HERE = Path(__file__).resolve().parent
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ID_KEYS = ("CLIENT_ID", "DEPARTMENT_ID", "PROGRAM_ID", "GROUP_ID")


class ApiError(Exception):
    pass


def load_env_file(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Config not found: {path} (copy perf.env.example to qat/perf.env or pre/perf.env and fill it in)")
    cfg = {}
    # utf-8-sig: Notepad / PowerShell 5.1 may save the file with a BOM, which would break the first KEY=value
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"{path}:{n}: expected KEY=value")
        k, v = line.split("=", 1)
        v = re.sub(r"(^|\s)#.*$", "", v)  # inline comment: "AGENT_IDS=   # optional"
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def call(base: str, method: str, path: str, body=None, token=None):
    """JSON request → (status, parsed body, refresh_token cookie or None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status, raw, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        status, raw, headers = e.code, e.read(), e.headers
    except urllib.error.URLError as e:
        raise ApiError(f"{method} {path}: cannot reach {base} ({e.reason})")
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = {"raw": raw[:300].decode(errors="replace")}
    refresh = None
    for c in headers.get_all("Set-Cookie") or []:
        jar = SimpleCookie()
        jar.load(c)
        if "refresh_token" in jar:
            refresh = jar["refresh_token"].value
    return status, parsed, refresh


def api_msg(body) -> str:
    return str(body.get("message") or body.get("detail") or body)[:300]


def login(base: str, email: str, force: bool):
    """Email OTP → optional MFA. Returns (access_token, refresh_token)."""
    status, body, _ = call(base, "POST", "/auth/login", {"email": email, "force_login": force})
    if status != 200:
        raise ApiError(f"Login failed: HTTP {status} {api_msg(body)}")
    data = body.get("data") or {}
    if data.get("force_login_required"):
        raise ApiError("User is logged in on another device. Set FORCE_LOGIN=true or log that session out.")
    print(f"OTP sent to {email}.")

    otp = input("Enter the OTP from the newest email: ").strip().replace(" ", "")
    status, body, refresh = call(base, "POST", "/auth/verify-otp", {"email": email, "otp": otp})
    if status != 200:
        raise ApiError(f"OTP verification failed: HTTP {status} {api_msg(body)}")
    data = body.get("data") or {}

    if data.get("access_token"):
        return data["access_token"], refresh

    if data.get("mfa_enabled") or data.get("pre_auth_token"):
        code = input("Enter authenticator (TOTP) code: ").strip().replace(" ", "")
        status, body, refresh = call(base, "POST", "/auth/mfa/verify-login", {"totp_code": code},
                                     token=data["pre_auth_token"])
        if status != 200:
            raise ApiError(f"MFA verification failed: HTTP {status} {api_msg(body)}")
        token = (body.get("data") or {}).get("access_token")
        if token:
            return token, refresh
    raise ApiError("Login response had no access token (MFA enrollment pending for this user?)")


def validate_ids(base: str, token: str, cfg: dict) -> None:
    """Group must exist in the user's tenant and match the given client/department/program.

    Needed up front because /calls/upload-zip resolves the group only in its background task:
    a wrong ID still returns 200 and then every one of the 5000 files fails silently.
    """
    for k in ID_KEYS:
        v = cfg.get(k, "")
        if v and not UUID_RE.match(v):
            raise ApiError(f"{k}={v!r} is not a valid UUID")
    for a in filter(None, (x.strip() for x in cfg.get("AGENT_IDS", "").split(","))):
        if not UUID_RE.match(a):
            raise ApiError(f"AGENT_IDS contains invalid UUID {a!r}")
    if not cfg.get("GROUP_ID") and not cfg.get("PROGRAM_ID"):
        raise ApiError("Set GROUP_ID (recommended) or PROGRAM_ID")

    if cfg.get("GROUP_ID"):
        status, body, _ = call(base, "GET", f"/groups/{cfg['GROUP_ID']}", token=token)
        if status == 403:
            print("WARNING: user can't read groups (needs admin/manager + departments:read) - "
                  "hierarchy NOT verified, continuing.")
        elif status != 200:
            raise ApiError(f"GROUP_ID check failed: HTTP {status} {api_msg(body)}")
        else:
            g = body.get("data") or {}
            mismatches = [
                f"{k}: config={cfg[k]} group has={g.get(field)}"
                for k, field in (("CLIENT_ID", "client_id"), ("DEPARTMENT_ID", "department_id"), ("PROGRAM_ID", "program_id"))
                if cfg.get(k) and str(g.get(field, "")).lower() != cfg[k].lower()
            ]
            if mismatches:
                raise ApiError("IDs don't match the group's hierarchy:\n  " + "\n  ".join(mismatches))
            print(f"Group OK: '{g.get('name')}' (client {g.get('client_id')}, dept {g.get('department_id')}, "
                  f"program {g.get('program_id')})")

    scope = f"group_ids={cfg['GROUP_ID']}" if cfg.get("GROUP_ID") else f"program_ids={cfg['PROGRAM_ID']}"
    status, body, _ = call(base, "GET", f"/qa/calls?{scope}&page=1&page_size=1", token=token)
    if status != 200:
        raise ApiError(f"Can't read the calls list for progress tracking: HTTP {status} {api_msg(body)}")
    total = ((body.get("data") or {}).get("pagination") or {}).get("total_items")
    print(f"Calls list OK - {total} existing calls in scope (baseline).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=HERE / "qat" / "perf.env")
    ap.add_argument("--check-only", action="store_true", help="login + validate IDs, don't run k6")
    ap.add_argument("k6_args", nargs="*", help="extra args for `k6 run` (put after --)")
    args = ap.parse_args()

    cfg = load_env_file(args.env_file)
    for k in ("BASE_URL", "PERF_EMAIL", "ZIP_PATH"):
        if not cfg.get(k):
            print(f"{k} is required in {args.env_file}", file=sys.stderr)
            return 2
    base = cfg["BASE_URL"].rstrip("/")
    if not args.check_only and not Path(cfg["ZIP_PATH"]).is_file():
        print(f"ZIP_PATH not found: {cfg['ZIP_PATH']}", file=sys.stderr)
        return 2

    try:
        token, refresh = login(base, cfg["PERF_EMAIL"], cfg.get("FORCE_LOGIN", "true").lower() == "true")
        print("Logged in." + ("" if refresh else " (no refresh cookie returned - run is limited to the 60 min token life)"))
        validate_ids(base, token, cfg)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.check_only:
        print("Check passed. Not running k6 (--check-only).")
        return 0

    k6 = shutil.which("k6")
    if not k6:
        print("k6 not found on PATH - install from https://grafana.com/docs/k6/latest/set-up/install-k6/", file=sys.stderr)
        return 2

    (HERE / cfg.get("RESULTS_DIR", "qat/results")).mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, **{k: v for k, v in cfg.items() if v and k not in ("PERF_EMAIL", "FORCE_LOGIN")}}
    child_env["ACCESS_TOKEN"] = token
    if refresh:
        child_env["REFRESH_TOKEN"] = refresh
    print("Starting k6 - don't log in as this user anywhere else until it finishes (single-session policy).\n")
    try:
        return subprocess.run([k6, "run", *args.k6_args, "zip_upload_5000.js"], cwd=HERE, env=child_env).returncode
    except KeyboardInterrupt:
        # k6 got the same Ctrl+C, wrote its summary and exited; server-side processing continues.
        print("\nStopped. QAT keeps processing the uploaded batch; re-attach with TRACK_ONLY (see README).")
        return 130


if __name__ == "__main__":
    sys.exit(main())

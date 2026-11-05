# k6 — 5000-call zip upload performance test

Measures, for one zip of N calls (default 5000) uploaded to `POST /api/calls/upload-zip`:

| Metric | Meaning |
|---|---|
| `zip_upload_seconds` | Time for the HTTP upload to be accepted (what the user waits on in the browser) |
| `first_call_ingested_seconds` / `all_calls_ingested_seconds` | Time from upload start until the 1st / Nth Call row exists (extraction → DB row → S3 → pipeline dispatch) |
| `first_call_evaluated_seconds` / `all_calls_evaluated_seconds` | Time until the 1st / Nth call is AI-scored (end-to-end, includes Celery/Lambda transcription + QA) |
| `ingest_throughput_calls_per_min`, `eval_throughput_calls_per_min` | Processing rate |
| `http_req_duration{scenario:site_browsing}` | How fast the site (health + Call Review list) responds for normal users **while** the batch runs |

### How completion is detected
The upload endpoint returns right away. The zip is then extracted and processed **one file at a time** in a FastAPI
`BackgroundTask` (`app/api/calls_upload_script/service.py::_extract_and_process_zip`), and there's no status endpoint.
The script takes a baseline of `GET /api/qa/calls?group_ids=<GROUP_ID>&page_size=1` (`total_items` for all calls, and for
`tab=evaluated_calls`), then polls every `POLL_INTERVAL_S` and subtracts the baseline.
**Use a dedicated test group** so other uploads don't inflate the counts.

## 1. Build the test zip

Real recordings (recommended, because synthetic audio doesn't give realistic transcription or scoring times). Use QAT-safe, non-PII audio:
```bash
python -I generate_test_zip.py --count 5000 --source-dir D:/qat_recordings --out calls_5000.zip
```
Synthetic (fast, good for testing the ingestion path only):
```bash
python -I generate_test_zip.py --count 5000 --seconds 20 --out calls_5000.zip
```
Each file gets a unique run-prefixed name, because the backend dedups by filename within a group and would clone instead of process.
The zip is stored uncompressed and has no folder entries, so it stays within the backend limits (≤ 5000 entries, compression ratio ≤ 100, ≤ 10 GB uncompressed, ≤ 5 GB upload).
Build a new zip for every run.

## Folder layout: QAT and PRE are kept apart
| Path | What |
|---|---|
| `qat/perf.env`, `qat/results/`, `qat/report/` | QAT config, every QAT result, log and report. The scripts use `qat/perf.env` by default. |
| `pre/perf.env`, `pre/results/` | PRE config and results. Start PRE runs with `pre/run_pre.ps1` (checks Lambda speed first). |
| `pre/run_pre.ps1`, `pre/pre_check.py` | PRE launcher and Lambda-speed check. |
| everything else here | Shared test scripts and report generators, the same for both environments. |

## 2. Configure the IDs (`qat/perf.env`)
Copy `perf.env.example` to `qat/perf.env` (or `pre/perf.env` for PRE), which git ignores. Fill in `BASE_URL`, `PERF_EMAIL`, `ZIP_PATH` and the target hierarchy:
`CLIENT_ID`, `DEPARTMENT_ID`, `PROGRAM_ID`, `GROUP_ID` (plus optional `AGENT_IDS`). All of them are sent to `/calls/upload-zip`.
The `GROUP_ID` decides where the calls land. The others must belong to that same group.

## 3. Run (recommended: `run_perf.py`)
```bash
python -I run_perf.py
```
It does three things, in order:
1. **Login.** It calls `/auth/login` for `PERF_EMAIL` and asks you for the emailed OTP (and the authenticator code, if MFA is on). The input is hidden.
   It gets the access token and `refresh_token` cookie. These are passed to k6 only through the child-process environment:
   they are never printed, written to disk, or put on the command line. k6 refreshes the token every 45 min, so multi-hour runs keep working.
2. **ID check.** It runs `GET /groups/{GROUP_ID}` and confirms the group's client, department and program match the ones you configured. It also checks
   that the user can read `/qa/calls`. This matters because the upload endpoint resolves the group **in its background task**: a wrong ID still returns
   200, and then all 5000 files fail silently.
3. **Start k6** with everything from `qat/perf.env`.

Use `python -I run_perf.py --check-only` to test the login and IDs without uploading. Extra k6 flags go after `--`, e.g. `python -I run_perf.py -- --out json=qat/results/raw.json`.

Perf user requirements: a **dedicated** user with role admin or manager and the `calls:read` and `departments:read` permissions (a supervisor works, but the hierarchy check is skipped).
Don't log in as that user anywhere else during the run. The single-session policy would invalidate the test's token.

### Alternative: plain k6 with a token you copied yourself
```bash
k6 run -e BASE_URL=https://<qat-api-host>/api -e ACCESS_TOKEN=<token> -e REFRESH_TOKEN=<refresh cookie> -e CLIENT_ID=<uuid> -e DEPARTMENT_ID=<uuid> -e PROGRAM_ID=<uuid> -e GROUP_ID=<uuid> -e ZIP_PATH=C:/perf/calls_5000.zip zip_upload_5000.js
```
This route skips the ID check, and the token ends up in your shell history.
The run can take hours. The console prints a progress line on every poll:
```
[+0h 42m 30s] ingested 2110/5000 (51.3/min, ETA 0h 56m 20s) | evaluated 1650/5000 (40.2/min, ETA 1h 23m 10s)
```
At the end you get a summary on stdout, plus `qat/results/zip_upload_<timestamp>.txt|.json`. Add `--out json=qat/results/raw.json` to keep every data point for graphs.

### Call sampling
If the program has call sampling on, most calls are **skipped** by the sampling gate and are never AI-scored.
The tracker reads the calls list's `meta.counts` (`not_reviewed` + `not_eligible`) and treats a call as **processed** once it is either
scored or skipped. Progress lines show `processed = scored + skipped by sampling`.

### Re-attach without uploading (`TRACK_ONLY`)
To check on a batch that's already uploaded, add `TRACK_ONLY=true`, `BASELINE_TOTAL`, `BASELINE_EVAL` and `BASELINE_SKIPPED`
(the counts from before the upload; the first run prints them as "Baseline in scope") and `BROWSE_VUS=0` to `qat/perf.env`, then run `run_perf.py`. It polls without uploading again.
In this mode, times are measured from when tracking starts, not from the original upload.

### Options (`-e KEY=value`)
| Key | Default | Notes |
|---|---|---|
| `BASE_URL` | — | API base **including `/api`** |
| `ACCESS_TOKEN` / `REFRESH_TOKEN` | — / none | see step 2 |
| `GROUP_ID` or `PROGRAM_ID` | — | `CLIENT_ID`, `DEPARTMENT_ID`, `AGENT_IDS` (comma-separated) optional, same as the API |
| `ZIP_PATH` | — | absolute path recommended |
| `EXPECTED_CALLS` | 5000 | number of audio files in the zip |
| `WAIT_FOR` | `evaluated` | `ingested` stops once all Call rows exist (much faster) |
| `EVAL_TARGET_RATIO` | 1.0 | lower it (e.g. 0.95) if call sampling is on for the program, because skipped calls never get evaluated |
| `POLL_INTERVAL_S` | 30 | |
| `MAX_WAIT_MIN` | 720 | hard cap |
| `FIRST_CALL_TIMEOUT_MIN` | 15 | stop if nothing appears (zip errors only show in backend logs) |
| `STALL_TIMEOUT_MIN` | 30 | stop if counts stop moving |
| `UPLOAD_TIMEOUT` | 60m | HTTP timeout for the upload request |
| `BROWSE_VUS` / `BROWSE_DURATION` | 5 / 30m | concurrent "normal users"; `BROWSE_VUS=0` disables. Keep the duration under the 60 min token life |
| `RESULTS_DIR` | qat/results | relative to perf/k6; PRE uses pre/results |

Thresholds (the run fails if crossed): run completed; browse p95 < 3 s; browse error rate < 2 %.

## End-to-end scenarios (`run_e2e.py`)
Sustained-rate tests: calls arrive as many small zips (`CALLS_PER_ZIP`, default 25) at a planned calls/min rate,
like many users uploading at once. k6 builds each zip in memory from `SOURCE_DIR` with unique filenames, so no zips are
pre-generated and nothing dedups against earlier runs. The runner tracks the pipeline during the upload phase and **keeps tracking afterwards until
every uploaded call is scored or skipped**, then writes `qat/results/e2e_<scenario>_<ts>.json` and `.html`.

| Scenario | Profile (PEAK_CALLS_PER_MIN=500) | Upload time | Calls |
|---|---|---|---|
| `smoke` | 50/min for 2 min. Run this first, every time | 2.5 min | 100 |
| `load` | ramp to 300/min (60% of peak) over 2 min, hold 15 min | 18 min | ~4,950 |
| `peak` | ramp to 500/min over 3 min, hold 10 min | 14 min | ~6,000 |
| `soak` | 250/min (50% of peak) for `SOAK_MINUTES` (60) | 64 min | ~15,500 |
| `spike` | 100/min baseline, jumps to 500/min in 30 s, holds 3 min, twice | 21 min | ~4,900 |

```bash
python -I run_e2e.py smoke
```
It prints the plan and asks you to type the scenario name, then asks for the OTP. Add `--yes` to skip the confirmation.
Override with `--peak 300` or `--soak-minutes 180`, or set them in `qat/perf.env`.

For one report covering several runs (with an execution matrix):
```bash
python -I e2e_report.py qat/results/e2e_load_*.json qat/results/e2e_peak_*.json qat/results/e2e_soak_*.json qat/results/e2e_spike_*.json -o qat/results/e2e_combined.html
```

**Graphs in the report** follow the standard load-test report set: JMeter dashboard, k6, Gatling and LoadRunner. Each graph has hover values and a "Show data" table.
k6 writes a per-request CSV (`qat/results/e2e_<scenario>_<ts>_k6.csv.gz`) that feeds the request-level graphs.

| Group | Graphs |
|---|---|
| Pipeline (end to end) | Cumulative planned vs created vs processed · Backlog · Pipeline throughput (calls/min, rolling 60 s) |
| Load & throughput | Active virtual users · Requests per second |
| Response times | Site response time over time (p50/p90/p95/p99) · Upload response time over time · Percentile distribution per page · Response-time histogram · Response time vs throughput (scatter) |
| Errors | Errors over time · Responses by status code |
| Latency & network | Latency breakdown per transaction (blocked/connect/TLS/send/wait/receive) · Network KB/s sent and received |
| Aggregate report | Per transaction: samples, avg/min/median/p90/p95/p99/max, error %, req/s, **Apdex** (T = 0.5 s for pages, 10 s for uploads) |

**Gates in the report:**
- upload success ≥ 99%
- planned load delivered ≥ 95%
- upload p95 ≤ 10 s
- call creation and scoring keep up with arrivals (≥ 90%)
- all calls processed
- site p95 ≤ 1 s with errors ≤ 1%

Pass/warn/fail feeds an overall verdict, and failing gates become findings with recommendations.

Notes:
- **Drain time:** today's Lambda pipeline scores ~35–40 calls/min, so any scenario above that builds a backlog. The drain phase
  measures how long it takes to clear (`MAX_DRAIN_MIN`, default 180). The "Scoring keeps up" gate will fail until capacity is raised.
- **Cost:** every eligible call is really transcribed and LLM-scored. All four scenarios together are ~31,000 calls.
- **Auth:** each k6 VU and the tracker refresh their own token (refresh tokens stay valid for 7 days), so multi-hour soaks work.
  Don't log in as the perf user elsewhere: the single-session policy would end the run.
- **Server-side metrics** (CPU, DB, Lambda, SQS) aren't collected. Pull them from CloudWatch for the run window.

## Burst sequences (`run_burst.py`)

```
python -I run_burst.py --sizes 100,200,500,1000 --yes             # one run per size
python -I run_burst.py --sizes 100,200,500,1000 --repeat 3 --yes  # typical value + range per size
python -I run_burst.py --sizes 1000 --stagger-s 30 --yes          # NON-standard: zips 30 s apart
```

- **Per-call verification** runs after each completed burst: the run's calls are listed from QAT's call list and
  compared with the report (`<run>_verify.md`). QAT doesn't keep uploaded file names and the zip upload back-dates
  each call's `created_at` by (index % 60) days, so the whole test group is read once and a run's calls are the uploads
  (`UPLOAD-…`) whose time of day falls in the run's window (earlier runs excluded by display number). Don't upload
  into that group during a test.
  `--no-verify` skips it; `verify_run.py <result.json> [more.json …]` checks older runs with one login.
  If QAT returns times without a zone, set `VERIFY_TZ_OFFSET_MIN` (e.g. 330 for IST) in `perf.env`.
- **Progress is polled every 10 s** (`BURST_POLL_INTERVAL_S`); the safety stop still fires after about a minute without answers.
- **Leftover calls don't count as progress.** At the start every run lists the group's unfinished calls (Call Review
  "under review" tab). When the group looks nearly done, the run counts only its own unfinished calls, so it is not
  declared finished while its own calls are still open. If some of its calls fail in the pipeline, it ends with
  "all calls finished; N failed in the pipeline" instead of waiting for them. This own-call check reads the whole
  "under review" list, so it runs only once all calls are uploaded, and then on every 2nd poll (`OWN_CHECK_EVERY`).

## Ramp / step tests (`run_e2e.py step`)

```
python -I run_e2e.py step --step-rates 100,200,500,1000 --step-minutes 5 --yes
```

- A step **fails** (uploads stop) when 2 zips are rejected, the API falls more than 10 zips behind in unpacking, or QAT stops answering.
- After each step it prints and records (`steps` in the result file) calls sent, created and analysed per minute, and
  whether **analysis kept up**: this run's own analysis backlog grew by no more than one minute of that step's calls.
  Set `STEP_STOP_ON_ANALYSIS=true` in `perf.env` to also stop the test at the first step where analysis falls behind.
- After any `run_e2e.py` scenario that finishes, the **per-call check runs automatically** (as for bursts); `--no-verify` skips it.
- **Each result records its setup** (git commit, k6 version, zips, poll interval, AI mode, load-generator host) under `setup`.
- **`burst_summary_<ts>.md`** ends every sequence: verdict per size (pass / pass with slow website / fail), typical time and
  range, per-call verification, and the change against earlier runs with the same size, zip packing and upload pattern.
  Rebuild with `python -I burst_summary.py qat/results/e2e_burst*_<date>_*.json -o <file>`.
- **`--stagger-s`** starts zip k after k × N seconds. It is gentler on the API, so its times are better but are not the
  standard "all at once" test: those runs are named `burst<N>stg<S>` and kept out of the standard comparisons and headline figures.

## What to watch on the server side during the run
- **API container CPU/memory and event-loop latency.** All 5000 files are processed inside the API process (not Celery):
  mutagen duration parsing runs synchronously on the event loop, and every file runs a `max(display_id)` query.
  Expect the `site_browsing` latency to rise. That's the "how my website reacts" number.
- **Deploys or restarts kill the batch.** BackgroundTasks aren't persisted, so a restart mid-run loses the remaining files with no error surfaced. Don't deploy QAT during a run.
- **Proxy limits on the upload.** ALB idle timeout (60 s default) and nginx `client_max_body_size` / `proxy_read_timeout` can fail
  multi-GB uploads, or the server-side copy of the spooled file after the upload. If `upload-zip` returns 413/502/504, look here first.
- **Celery or Lambda transcription queue depth, DB connections, and S3 PUT rate.** These drive `all_calls_evaluated_seconds`.
- **Cost.** 5000 real calls run 5000 transcription and LLM evaluations. Check the LLM/transcription budget before running on QAT, and never run against PROD.

## Cleanup
The test creates 5000 Call rows (plus CallAnalysis/CallViolation, S3 objects and pipeline outputs) in the test group.
Soft-delete them, or delete the test group afterwards, so they don't skew QAT dashboards.

## Rollback / risk
The script only uploads to the configured group and reads call counts, and it changes no config.
To stop a run, press Ctrl+C in k6. Note this does **not** stop the server-side background processing, which keeps going until the zip is done or the API restarts.

# Perf testing handoff — zip call upload (from the QAT pilot session, 2026-10-28)

Read this first in a new chat. Then read `README.md` in this folder for how to run things.

## What exists (all in `BPO-BOX-Backend-QAT/perf/k6/`)
| File | Purpose |
|---|---|
| `zip_upload_5000.js` | k6 test: uploads a zip to `POST /api/calls/upload-zip`, then polls `GET /api/qa/calls` (group-scoped `total_items`, `tab=evaluated_calls`, `meta.counts`) to track ingestion and scoring to completion. 5 "browse" VUs measure site latency during processing. Supports `TRACK_ONLY=true` to re-attach to an uploaded batch without re-uploading. |
| `run_perf.py` | Launcher: email-OTP login (+ MFA TOTP), checks CLIENT/DEPARTMENT/PROGRAM/GROUP IDs match via `GET /api/groups/{id}`, then runs k6 with the token passed via env only. `--check-only` = login + validation, no upload. |
| `generate_test_zip.py` | Builds an N-call zip from a folder of recordings (unique run-prefixed names, STORED, no dir entries). |
| `qat/perf.env` | QAT config (git-ignored), the scripts' default. **Currently in TRACK_ONLY mode — remove that block before a real run.** PRE has its own: `pre/perf.env`. |
| `perf.env.example` | Template. |
| `qat/results/` | All QAT results, logs and reports. PRE results go to `pre/results/`. |

Test audio: `C:\Users\Admin\Downloads\bpo-perf\source\` (100 MP3s, 8 are < 30 s), zips `calls_pilot_100.zip` (33 MB) and `calls_5000.zip` (1.65 GB) in `C:\Users\Admin\Downloads\bpo-perf\`. Build fresh zips per run (new `--prefix`) — the backend dedups by filename within a group.

## Running against PRE
Use `pre/perf.env` (already pointed at `https://pre.bpobox.ai/api`) and `pre/run_pre.ps1`. For the older run_perf.py flow:
1. Fill in `pre/perf.env`; `BASE_URL` is set (the API lives at `<frontend host>/api`; verify with `GET <host>/api/health/`), and PRE's CLIENT/DEPARTMENT/PROGRAM/GROUP/AGENT IDs (QAT IDs won't exist on PRE). Remove the TRACK_ONLY block.
2. `python -I run_perf.py --env-file pre/perf.env --check-only` → enter the OTP in the terminal (visible prompt). OTP requests are rate-limited to 1/min; 5 wrong attempts lock out.
3. Pilot with 100 calls first, then 5000.
- Use a **dedicated perf user** (admin/manager + `calls:read`, `departments:read`) and an **empty dedicated group**. Single-session policy: logging in elsewhere (incl. another run_perf) kills the running test's token.
- Don't deploy during a run (zip processing is a FastAPI BackgroundTask — a restart drops the rest of the batch).
- Real recordings ⇒ real AssemblyAI + LLM cost per eligible call.

## QAT pilot results (100 calls, group 'Outbound Sales – Hindi')
- Upload 33 MB: **6 s**. All 100 Call rows created: **~1 min**. First call scored: 2 min.
- Scored after ~25 min: **24 / 92 eligible**; 8 skipped as `not_eligible` (< 30 s). **No sampling skips** (sampling was ruled out by `meta.counts`).
- Scoring throughput **~1.1 calls/min** ⇒ ~70 h for 5000 calls at QAT capacity.
- Site during processing: p50 290 ms, p95 330 ms, max 1.16 s, 0 errors / 3772 requests.

## Code / infra findings (verified in code unless marked)
1. Zip processing runs sequentially in a FastAPI `BackgroundTask` inside gunicorn (`calls_upload_script/service.py::_extract_and_process_zip`) — not durable, competes with web traffic.
2. Each call queues 3+ Celery tasks on the one FIFO `qa_analysis` queue (transcribe → sentiment/scoring + violations → `calculate_ai_kpis`); a big batch head-of-line blocks itself and all live Dialpad/Twilio/TeleCMI scoring. Downstream dispatches hard-code `queue='qa_analysis'` (`dialpad/tasks.py:_dialpad_queue_sentiment_and_violations`, `twilio/tasks.py` `calculate_ai_kpis` send).
3. `expires=3600` on pipeline tasks silently drops calls queued > 1 h. **Fixed locally for the upload path only** (not deployed); webhook paths still have it.
4. Errors after "200 processing started" are log-only; no batch id/status endpoint; `/calls/retry-failed` reads `bpobox_failed_files.log` which nothing writes.
5. Per-call `max(cast(display_id))` + `LIKE '%/filename'` dedup scans; `display_id` not unique and its lock is per-process (dup ids possible across gunicorn workers).
6. `get_audio_duration` (mutagen) runs sync on the event loop.
7. Zip stored twice on disk + full extraction to /tmp.
8. Infra (inferred from `QAT/application_start.sh`, not verified on servers): gunicorn + celery + nginx on one host per env; celery `--concurrency=4`, `prefetch=1`; Redis is broker + backend (check ElastiCache/persistence); ALB idle timeout / nginx body limits untested for multi-GB uploads; no queue-depth/task-age alarms.

## Local changes NOT deployed anywhere (folder is not a git repo)
- `app/api/calls_upload_script/router.py`: `_resolve_tenant_id` — non-super_admin can't upload into another tenant (was a cross-tenant write). Test: `tests/unit/test_calls_upload_tenant_scope.py` (not run under pytest here; logic verified in isolation).
- `app/api/calls_upload_script/service.py`: removed `expires=3600` from the upload pipeline `apply_async`. Test: `tests/unit/test_calls_upload_no_task_expiry.py` (passes with `--noconftest`).
- PRE (and QAT) still run the old code ⇒ PRE results will include the 1 h expiry loss for big batches.

## Update 2026-10-29/30
- QAT switched to the Lambda pipeline: 100-call pilot scored 82–88/92 in ~2.5 min (~35–40 calls/min; capped by transcription_svc reserved concurrency 50 × ~75 s), stragglers finish on SQS retry. Site p95 395 ms, 0 errors.
- New end-to-end scenario tooling: `run_e2e.py` (smoke/load/peak/soak/spike at PEAK_CALLS_PER_MIN, default 500), `e2e_scenarios.js` (k6 builds zips in memory), `e2e_report.py` (HTML report, combinable). See README "End-to-end scenarios". Tested against a local mock only, not yet on QAT.
- Sign-off report template (sample data): `report/performance_signoff_report.html`.

## In progress when this session ended
"Separate upload queue": route the upload-originated chain to its own queue (e.g. `bulk_upload`) so batches don't block live calls. Idea: in `fetch_dialpad_recording_and_transcribe`, read `self.request.delivery_info['routing_key']`, put it in a ContextVar, and have `_dialpad_queue_sentiment_and_violations` (and the `calculate_ai_kpis` send in `twilio/tasks.py`) use it when it's the bulk queue; upload path sends with `queue='bulk_upload'`; add the queue to the worker (`celery_manager.py QUEUE_NAMES`, server's `celery-qat.service`) ideally on separate workers. **No code changed for this yet.**

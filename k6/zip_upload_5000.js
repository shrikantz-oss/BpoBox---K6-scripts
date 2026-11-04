/**
 * BPO-BOX — Zip call upload performance test (k6)
 *
 * What it measures
 *   1. How long POST /api/calls/upload-zip takes for a zip of N calls (default 5000).
 *   2. How long the backend takes to ingest every call (Call rows created, S3 upload,
 *      pipeline dispatch) — tracked by polling GET /api/qa/calls total_items.
 *   3. How long until every call is AI-evaluated (tab=evaluated_calls count).
 *   4. How the website/API responds for normal users WHILE the batch is processing
 *      (scenario "site_browsing" hits the same endpoints the Call Review page uses).
 *
 * Why polling: /calls/upload-zip returns as soon as the zip is saved; extraction and
 * per-file processing run sequentially in a FastAPI BackgroundTask with no status
 * endpoint (app/api/calls_upload_script/service.py::_extract_and_process_zip).
 * So completion is measured by counting calls in the target group before vs after.
 *
 * Run (see README.md for all options):
 *   k6 run -e BASE_URL=https://<qat-api-host>/api -e ACCESS_TOKEN=... \
 *          -e GROUP_ID=<uuid> -e ZIP_PATH=C:/perf/calls_5000.zip zip_upload_5000.js
 */
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Gauge, Rate } from 'k6/metrics';
import { open as fsOpen } from 'k6/experimental/fs';

// ─── Config (all via -e KEY=value) ────────────────────────────────────────────
const env = (k, d) => (__ENV[k] !== undefined && __ENV[k] !== '' ? __ENV[k] : d);

const BASE_URL = env('BASE_URL', '').replace(/\/+$/, ''); // must include /api
const ACCESS_TOKEN = env('ACCESS_TOKEN', '');
const REFRESH_TOKEN = env('REFRESH_TOKEN', ''); // optional: value of the refresh_token cookie
const ZIP_PATH = env('ZIP_PATH', '');

const GROUP_ID = env('GROUP_ID', '');
const PROGRAM_ID = env('PROGRAM_ID', '');
const CLIENT_ID = env('CLIENT_ID', '');
const DEPARTMENT_ID = env('DEPARTMENT_ID', '');
const AGENT_IDS = env('AGENT_IDS', ''); // comma-separated UUIDs, optional

const EXPECTED_CALLS = parseInt(env('EXPECTED_CALLS', '5000'), 10);
// ingested = Call rows exist; evaluated = AI scoring finished (slower, end-to-end)
const WAIT_FOR = env('WAIT_FOR', 'evaluated');
// Fraction of EXPECTED_CALLS that counts as "evaluated done" (lower it if call sampling skips some)
const EVAL_TARGET_RATIO = parseFloat(env('EVAL_TARGET_RATIO', '1.0'));

const POLL_INTERVAL_S = parseInt(env('POLL_INTERVAL_S', '30'), 10);
const MAX_WAIT_MIN = parseInt(env('MAX_WAIT_MIN', '720'), 10); // 12h hard cap
const FIRST_CALL_TIMEOUT_MIN = parseInt(env('FIRST_CALL_TIMEOUT_MIN', '15'), 10);
const STALL_TIMEOUT_MIN = parseInt(env('STALL_TIMEOUT_MIN', '30'), 10);
const UPLOAD_TIMEOUT = env('UPLOAD_TIMEOUT', '60m');
const TOKEN_REFRESH_MIN = parseInt(env('TOKEN_REFRESH_MIN', '45'), 10); // access token lives 60 min

const BROWSE_VUS = parseInt(env('BROWSE_VUS', '5'), 10);
const BROWSE_DURATION = env('BROWSE_DURATION', '30m'); // keep < token lifetime (60m) unless token is long-lived
const RESULTS_DIR = env('RESULTS_DIR', 'qat/results');
// Re-attach to an already-uploaded batch without uploading again (see README).
const TRACK_ONLY = env('TRACK_ONLY', 'false') === 'true';

if (!BASE_URL || !ACCESS_TOKEN || !ZIP_PATH || (!GROUP_ID && !PROGRAM_ID)) {
  throw new Error('Required: BASE_URL, ACCESS_TOKEN, ZIP_PATH and GROUP_ID (or PROGRAM_ID). See README.md');
}

// fs.open shares the bytes across VUs (unlike open()), so browse VUs don't each hold a copy.
const zipFile = await fsOpen(ZIP_PATH);
const ZIP_NAME = ZIP_PATH.split(/[\\/]/).pop();

// ─── Custom metrics ──────────────────────────────────────────────────────────
const zipUploadSeconds = new Trend('zip_upload_seconds');
const firstCallIngestedSeconds = new Trend('first_call_ingested_seconds');
const allCallsIngestedSeconds = new Trend('all_calls_ingested_seconds');
const firstCallEvaluatedSeconds = new Trend('first_call_evaluated_seconds');
const allCallsEvaluatedSeconds = new Trend('all_calls_evaluated_seconds');
const callsIngested = new Gauge('calls_ingested');
const callsEvaluated = new Gauge('calls_evaluated');
const callsSkipped = new Gauge('calls_skipped_by_sampling');
const ingestPerMin = new Gauge('ingest_throughput_calls_per_min');
const evalPerMin = new Gauge('eval_throughput_calls_per_min');
const pollErrors = new Counter('progress_poll_errors');
const runCompleted = new Rate('run_completed');
const browseOk = new Rate('browse_success');

// ─── Scenarios ───────────────────────────────────────────────────────────────
const scenarios = {
  upload_and_track: {
    executor: 'per-vu-iterations',
    exec: 'uploadAndTrack',
    vus: 1,
    iterations: 1,
    maxDuration: `${MAX_WAIT_MIN + 90}m`, // wait cap + upload time + buffer
    tags: { flow: 'upload' },
  },
};
if (BROWSE_VUS > 0) {
  scenarios.site_browsing = {
    executor: 'constant-vus',
    exec: 'browse',
    vus: BROWSE_VUS,
    duration: BROWSE_DURATION,
    startTime: '10s', // let the baseline count land first
    tags: { flow: 'browse' },
  };
}

export const options = {
  scenarios,
  setupTimeout: '2m',
  thresholds: {
    run_completed: ['rate==1'],
    'http_req_duration{scenario:site_browsing}': ['p(95)<3000'],
    'http_req_failed{scenario:site_browsing}': ['rate<0.02'],
    browse_success: ['rate>0.98'],
  },
  summaryTrendStats: ['min', 'avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// ─── Helpers ─────────────────────────────────────────────────────────────────
function scopeQuery() {
  // Prefer the exact group; program scope also counts other groups' calls.
  if (GROUP_ID) return `group_ids=${GROUP_ID}`;
  return `program_ids=${PROGRAM_ID}`;
}

function authParams(token, name, extra = {}) {
  return { headers: { Authorization: `Bearer ${token}` }, tags: { name }, timeout: '120s', ...extra };
}

function fmtDur(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return 'n/a';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${String(m).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`;
}

// Session holder for the tracker VU (the only VU allowed to refresh — refresh rotates the cookie).
const session = { token: ACCESS_TOKEN, refresh: REFRESH_TOKEN, issuedAt: Date.now() };

function refreshToken() {
  if (!session.refresh) return false;
  const res = http.post(`${BASE_URL}/auth/refresh`, null, {
    cookies: { refresh_token: { value: session.refresh, replace: true } },
    tags: { name: 'POST /auth/refresh' },
  });
  if (res.status !== 200) {
    console.error(`Token refresh failed: HTTP ${res.status} ${res.body && String(res.body).slice(0, 200)}`);
    return false;
  }
  try {
    session.token = res.json('data.access_token');
  } catch (e) {
    console.error(`Token refresh returned non-JSON body: ${e}`);
    return false;
  }
  const rotated = res.cookies.refresh_token;
  if (rotated && rotated.length) session.refresh = rotated[0].value;
  session.issuedAt = Date.now();
  console.log('Access token refreshed');
  return true;
}

/** GET with one transparent retry after a token refresh on 401. */
function trackerGet(url, name) {
  let res = http.get(url, authParams(session.token, name));
  if (res.status === 401 && refreshToken()) res = http.get(url, authParams(session.token, name));
  return res;
}

/**
 * Calls in scope: { total, skipped } or null on error. skipped = calls the sampling gate
 * won't send to AI (not_reviewed = not sampled, not_eligible = too short / no recording),
 * read from the list's meta.counts; these never reach tab=evaluated_calls.
 */
function countCallsWithSampling() {
  const res = trackerGet(`${BASE_URL}/qa/calls?${scopeQuery()}&page=1&page_size=1`, 'GET /qa/calls count (all)');
  if (res.status !== 200) {
    pollErrors.add(1);
    console.warn(`Count poll failed (all): HTTP ${res.status} ${String(res.body || '').slice(0, 200)}`);
    return null;
  }
  try {
    const total = res.json('data.pagination.total_items');
    const counts = res.json('data.meta.counts') || {};
    if (typeof total !== 'number') return null;
    return { total, skipped: (counts.not_reviewed || 0) + (counts.not_eligible || 0) };
  } catch (e) {
    pollErrors.add(1);
    console.warn(`Count poll returned unparseable body: ${e}`);
    return null;
  }
}

/** Returns total_items for the scope (optionally a tab), or null on error. */
function countCalls(tab) {
  const tabQ = tab ? `&tab=${tab}` : '';
  const res = trackerGet(
    `${BASE_URL}/qa/calls?${scopeQuery()}&page=1&page_size=1${tabQ}`,
    tab ? `GET /qa/calls count (${tab})` : 'GET /qa/calls count (all)',
  );
  if (res.status !== 200) {
    pollErrors.add(1);
    console.warn(`Count poll failed (${tab || 'all'}): HTTP ${res.status} ${String(res.body || '').slice(0, 200)}`);
    return null;
  }
  try {
    const n = res.json('data.pagination.total_items');
    return typeof n === 'number' ? n : null;
  } catch (e) {
    pollErrors.add(1);
    console.warn(`Count poll returned unparseable body: ${e}`);
    return null;
  }
}

async function readWholeFile(file) {
  const info = await file.stat();
  const out = new Uint8Array(info.size);
  const CHUNK = 8 * 1024 * 1024;
  let offset = 0;
  while (offset < info.size) {
    const chunk = new Uint8Array(Math.min(CHUNK, info.size - offset));
    const n = await file.read(chunk);
    if (n === null || n === 0) break;
    out.set(chunk.subarray(0, n), offset);
    offset += n;
  }
  if (offset !== info.size) throw new Error(`Read ${offset}/${info.size} bytes of ${ZIP_PATH}`);
  return out;
}

// ─── Scenario 1: upload the zip, then track processing to completion ─────────
export async function uploadAndTrack() {
  // Baseline so pre-existing calls in the group aren't counted. TRACK_ONLY re-attaches to a
  // batch already uploaded: pass the pre-upload baseline instead of measuring it now.
  let baseTotal, baseEval, baseSkipped;
  if (TRACK_ONLY) {
    baseTotal = parseInt(env('BASELINE_TOTAL', '0'), 10);
    baseEval = parseInt(env('BASELINE_EVAL', '0'), 10);
    baseSkipped = parseInt(env('BASELINE_SKIPPED', '0'), 10);
  } else {
    const base = countCallsWithSampling();
    baseEval = countCalls('evaluated_calls');
    if (base === null || baseEval === null) {
      runCompleted.add(false);
      console.error('Could not read baseline call counts — check token, role (admin/supervisor/manager + calls:read) and GROUP_ID.');
      return;
    }
    baseTotal = base.total;
    baseSkipped = base.skipped;
  }
  console.log(`Baseline in scope: ${baseTotal} calls total, ${baseEval} evaluated, ${baseSkipped} skipped by sampling`);

  let uploadSec = 0;
  const startedAt = Date.now(); // "user clicked upload" (TRACK_ONLY: tracking start)
  if (!TRACK_ONLY) {
    const ok = await uploadZip(startedAt);
    if (ok === null) return;
    uploadSec = ok;
  } else {
    console.log('TRACK_ONLY: skipping upload, times below are relative to now, not to the original upload.');
  }

  const evalTarget = Math.ceil(EXPECTED_CALLS * EVAL_TARGET_RATIO);
  const deadline = startedAt + MAX_WAIT_MIN * 60000;
  let lastProgressAt = Date.now();
  let lastIngested = 0;
  let lastEvaluated = 0;
  let lastSkipped = 0;
  let firstIngestAt = null, ingestDoneAt = null, firstEvalAt = null, evalDoneAt = null;
  let reason = 'max wait reached';
  // TRACK_ONLY attaches mid-batch: rates count only progress made since tracking started.
  let rateBase = TRACK_ONLY ? null : { ingested: 0, processed: 0 };
  const runMinutes = (t) => Math.max((t - startedAt) / 60000, 1 / 60);

  for (let poll = 0; ; poll++) {
    if (!(TRACK_ONLY && poll === 0)) sleep(POLL_INTERVAL_S);
    if (Date.now() - session.issuedAt > TOKEN_REFRESH_MIN * 60000) refreshToken();

    const all = countCallsWithSampling();
    const evald = countCalls('evaluated_calls');
    const now = Date.now();
    const elapsed = (now - startedAt) / 1000;
    const mins = Math.max(elapsed / 60, 1 / 60);

    if (all !== null && evald !== null) {
      const ingested = Math.max(0, all.total - baseTotal);
      const evaluated = Math.max(0, evald - baseEval);
      const skipped = Math.max(0, all.skipped - baseSkipped);
      callsIngested.add(ingested);
      callsEvaluated.add(evaluated);
      callsSkipped.add(skipped);
      if (ingested > lastIngested || evaluated > lastEvaluated || skipped > lastSkipped) lastProgressAt = now;
      lastIngested = ingested;
      lastEvaluated = evaluated;
      lastSkipped = skipped;

      // A call is "processed" once scored OR deliberately skipped by sampling.
      const processed = evaluated + skipped;
      if (!firstIngestAt && ingested > 0) { firstIngestAt = now; firstCallIngestedSeconds.add(elapsed); }
      if (!firstEvalAt && evaluated > 0) { firstEvalAt = now; firstCallEvaluatedSeconds.add(elapsed); }
      if (!ingestDoneAt && ingested >= EXPECTED_CALLS) { ingestDoneAt = now; allCallsIngestedSeconds.add(elapsed); }
      if (!evalDoneAt && processed >= evalTarget) { evalDoneAt = now; allCallsEvaluatedSeconds.add(elapsed); }

      // Rates over the whole run (from upload start), so the first poll isn't inflated.
      if (!rateBase) rateBase = { ingested, processed };
      const iRate = (ingested - rateBase.ingested) / mins;
      const pRate = (processed - rateBase.processed) / mins;
      // Updated every poll so a stopped run (Ctrl+C) still reports throughput.
      ingestPerMin.add(ingested / runMinutes(ingestDoneAt || now));
      evalPerMin.add(processed / runMinutes(evalDoneAt || now));
      const etaIngest = iRate > 0 && !ingestDoneAt ? ((EXPECTED_CALLS - ingested) / iRate) * 60 : null;
      const etaEval = pRate > 0 && !evalDoneAt ? ((evalTarget - processed) / pRate) * 60 : null;
      console.log(
        `[+${fmtDur(elapsed)}] ingested ${ingested}/${EXPECTED_CALLS} (${iRate.toFixed(1)}/min${etaIngest ? `, ETA ${fmtDur(etaIngest)}` : ''})` +
        ` | processed ${processed}/${evalTarget} = scored ${evaluated} + skipped by sampling ${skipped}` +
        ` (${pRate.toFixed(1)}/min${etaEval ? `, ETA ${fmtDur(etaEval)}` : ''})`,
      );

      if (WAIT_FOR === 'ingested' && ingestDoneAt) { reason = 'all calls ingested'; break; }
      if (WAIT_FOR !== 'ingested' && evalDoneAt) { reason = 'all calls scored or skipped by sampling'; break; }
    }

    // Background extraction errors (bad zip, >5000 entries, size limits) never reach the
    // HTTP response — they only show in backend logs — so "nothing appeared" is the signal.
    if (!firstIngestAt && now - startedAt > FIRST_CALL_TIMEOUT_MIN * 60000) {
      reason = `no call appeared within ${FIRST_CALL_TIMEOUT_MIN} min (check backend logs for "Zip extraction failed")`;
      break;
    }
    if (firstIngestAt && now - lastProgressAt > STALL_TIMEOUT_MIN * 60000) {
      reason = `no progress for ${STALL_TIMEOUT_MIN} min (stalled — check API/Celery logs)`;
      break;
    }
    if (now > deadline) break;
  }

  const endAt = Date.now();

  const done = WAIT_FOR === 'ingested' ? !!ingestDoneAt : !!evalDoneAt;
  runCompleted.add(done);
  console.log(
    `\n==== RESULT: ${done ? 'COMPLETED' : 'NOT COMPLETED'} — ${reason}\n` +
    `  zip upload (HTTP)        : ${fmtDur(uploadSec)}\n` +
    `  first call ingested      : ${firstIngestAt ? fmtDur((firstIngestAt - startedAt) / 1000) : 'n/a'}\n` +
    `  all calls ingested       : ${ingestDoneAt ? fmtDur((ingestDoneAt - startedAt) / 1000) : `not reached (${lastIngested}/${EXPECTED_CALLS})`}\n` +
    `  first call scored        : ${firstEvalAt ? fmtDur((firstEvalAt - startedAt) / 1000) : 'n/a'}\n` +
    `  all calls processed      : ${evalDoneAt ? fmtDur((evalDoneAt - startedAt) / 1000) : `not reached (${lastEvaluated + lastSkipped}/${evalTarget})`}\n` +
    `  scored / skipped (sampl.): ${lastEvaluated} / ${lastSkipped}\n` +
    `  total tracked time       : ${fmtDur((endAt - startedAt) / 1000)}\n`,
  );
}

/** POST the zip; returns upload seconds, or null on failure. */
async function uploadZip(startedAt) {
  const bytes = await readWholeFile(zipFile);
  const sizeMb = (bytes.length / 1024 / 1024).toFixed(1);
  console.log(`Uploading ${ZIP_NAME} (${sizeMb} MB, expecting ${EXPECTED_CALLS} calls)…`);

  const form = { file: http.file(bytes.buffer, ZIP_NAME, 'application/zip') };
  if (GROUP_ID) form.group_id = GROUP_ID;
  if (PROGRAM_ID) form.program_id = PROGRAM_ID;
  if (CLIENT_ID) form.client_id = CLIENT_ID;
  if (DEPARTMENT_ID) form.department_id = DEPARTMENT_ID;
  if (AGENT_IDS) form.agent_ids = AGENT_IDS;

  if (Date.now() - session.issuedAt > TOKEN_REFRESH_MIN * 60000) refreshToken();
  const up = http.post(`${BASE_URL}/calls/upload-zip`, form, authParams(session.token, 'POST /calls/upload-zip', { timeout: UPLOAD_TIMEOUT }));
  const uploadSec = (Date.now() - startedAt) / 1000;
  zipUploadSeconds.add(uploadSec);

  const uploadOk = check(up, {
    'upload-zip returned 200': (r) => r.status === 200,
    'upload-zip says processing started': (r) => String(r.body || '').includes('processing started'),
  });
  if (!uploadOk) {
    runCompleted.add(false);
    console.error(`Upload failed after ${fmtDur(uploadSec)}: HTTP ${up.status} ${String(up.body || up.error || '').slice(0, 500)}`);
    return null;
  }
  console.log(`Upload accepted in ${fmtDur(uploadSec)} (${(sizeMb / Math.max(uploadSec, 0.001)).toFixed(2)} MB/s). Tracking processing…`);
  return uploadSec;
}

// ─── Scenario 2: normal users using the site while the batch runs ────────────
export function browse() {
  const p = (name) => authParams(ACCESS_TOKEN, name, { timeout: '60s' });
  const responses = http.batch([
    ['GET', `${BASE_URL}/health/`, null, { tags: { name: 'GET /health' }, timeout: '30s' }],
    ['GET', `${BASE_URL}/health/db-health`, null, { tags: { name: 'GET /health/db-health' }, timeout: '30s' }],
  ]);
  responses.forEach((r) => browseOk.add(r.status === 200));

  // Call Review page: evaluated tab, then pending tab (what reviewers look at during ingestion)
  const evaluated = http.get(`${BASE_URL}/qa/calls?${scopeQuery()}&page=1&page_size=10&sort_by=date:desc&tab=evaluated_calls`, p('GET /qa/calls (evaluated page)'));
  browseOk.add(check(evaluated, { 'calls list (evaluated) 200': (r) => r.status === 200 }));
  sleep(1 + Math.random() * 2);

  const pending = http.get(`${BASE_URL}/qa/calls?${scopeQuery()}&page=1&page_size=10&sort_by=date:desc&tab=call_under_review`, p('GET /qa/calls (pending page)'));
  browseOk.add(check(pending, { 'calls list (pending) 200': (r) => r.status === 200 }));

  if (evaluated.status === 401 || pending.status === 401) {
    console.warn('Browse VU got 401 — ACCESS_TOKEN expired or session replaced (single-session policy).');
  }
  sleep(2 + Math.random() * 3); // think time
}

// ─── Report ──────────────────────────────────────────────────────────────────
export function handleSummary(data) {
  const m = data.metrics;
  const val = (name, stat = 'max') => (m[name] && m[name].values ? m[name].values[stat] : undefined);
  const ms = (v) => (v === undefined ? 'n/a' : `${Math.round(v)} ms`);
  const browseDur = m['http_req_duration{scenario:site_browsing}'];
  const browseFail = m['http_req_failed{scenario:site_browsing}'];

  const lines = [
    '',
    '══════════ BPO-BOX zip upload performance ══════════',
    `Zip                       : ${ZIP_NAME}  (expected ${EXPECTED_CALLS} calls, wait-for=${WAIT_FOR})`,
    `Completed                 : ${val('run_completed', 'rate') === 1 ? 'YES' : 'NO'}`,
    `Zip upload (HTTP)         : ${fmtDur(val('zip_upload_seconds'))}`,
    `First call ingested       : ${fmtDur(val('first_call_ingested_seconds'))}`,
    `All calls ingested        : ${fmtDur(val('all_calls_ingested_seconds'))}`,
    `First call scored         : ${fmtDur(val('first_call_evaluated_seconds'))}`,
    `All calls processed       : ${fmtDur(val('all_calls_evaluated_seconds'))}  (scored or skipped by sampling)`,
    `Ingested / scored / skipped: ${val('calls_ingested', 'value') ?? 'n/a'} / ${val('calls_evaluated', 'value') ?? 'n/a'} / ${val('calls_skipped_by_sampling', 'value') ?? 'n/a'}`,
    `Ingest throughput         : ${(val('ingest_throughput_calls_per_min', 'value') ?? 0).toFixed(1)} calls/min`,
    `Processing throughput     : ${(val('eval_throughput_calls_per_min', 'value') ?? 0).toFixed(1)} calls/min`,
    `Progress poll errors      : ${val('progress_poll_errors', 'count') ?? 0}`,
    '',
    '── Website responsiveness during processing (site_browsing) ──',
    `p50 / p95 / p99 / max     : ${browseDur ? [ms(browseDur.values.med), ms(browseDur.values['p(95)']), ms(browseDur.values['p(99)']), ms(browseDur.values.max)].join(' / ') : 'n/a (BROWSE_VUS=0)'}`,
    `HTTP error rate           : ${browseFail ? (browseFail.values.rate * 100).toFixed(2) + '%' : 'n/a'}`,
    `Requests                  : ${val('http_reqs', 'count') ?? 0} total`,
    '',
  ];
  const text = lines.join('\n');
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return {
    stdout: text,
    [`${RESULTS_DIR}/zip_upload_${stamp}.json`]: JSON.stringify(data, null, 2),
    [`${RESULTS_DIR}/zip_upload_${stamp}.txt`]: text,
  };
}

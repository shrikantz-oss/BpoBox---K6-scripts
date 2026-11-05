/**
 * BPO-BOX — end-to-end scenario load (smoke / load / peak / soak / spike).
 *
 * Arrival model: calls arrive as many small zips (CALLS_PER_ZIP calls each) at a
 * controlled rate, like many users uploading at once. Each iteration builds a fresh
 * STORED zip in memory from the source recordings, with unique filenames, so the
 * backend never dedups against earlier runs.
 *
 * Launch through run_e2e.py — it logs in, plans the scenario, passes SCENARIO_JSON
 * (stages in calls/min), tracks the pipeline until every call is processed, and
 * writes the HTML report. This script only generates load and measures the API.
 */
import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';
import { open as fsOpen, SeekMode } from 'k6/experimental/fs';

const env = (k, d) => (__ENV[k] !== undefined && __ENV[k] !== '' ? __ENV[k] : d);
const BASE_URL = env('BASE_URL', '').replace(/\/+$/, '');
const ACCESS_TOKEN = env('ACCESS_TOKEN', '');
const REFRESH_TOKEN = env('REFRESH_TOKEN', '');
const GROUP_ID = env('GROUP_ID', '');
const PROGRAM_ID = env('PROGRAM_ID', '');
const CLIENT_ID = env('CLIENT_ID', '');
const DEPARTMENT_ID = env('DEPARTMENT_ID', '');
const AGENT_IDS = env('AGENT_IDS', '');
const RUN_ID = env('RUN_ID', `run${Date.now()}`);
const CALLS_PER_ZIP = parseInt(env('CALLS_PER_ZIP', '25'), 10);
const MANIFEST = env('SOURCE_MANIFEST', '');
const SUMMARY_PATH = env('K6_SUMMARY_PATH', 'qat/results/e2e_k6_summary.json');
const BROWSE_VUS = parseInt(env('BROWSE_VUS', '5'), 10);
const TOKEN_REFRESH_MIN = 45; // access token lives 60 min

// {name, stages: [{minutes, callsPerMin}], startCallsPerMin}
const PLAN = JSON.parse(env('SCENARIO_JSON', '{}'));
if (!BASE_URL || !ACCESS_TOKEN || !GROUP_ID || !MANIFEST || !PLAN.stages) {
  throw new Error('Run via run_e2e.py (needs BASE_URL, ACCESS_TOKEN, GROUP_ID, SOURCE_MANIFEST, SCENARIO_JSON)');
}

// ─── Source recordings (bytes shared across VUs via fs; CRCs precomputed) ─────
const SOURCES = JSON.parse(open(MANIFEST)); // [{path, size, crc32}]
const FILES = [];
for (const s of SOURCES) FILES.push(await fsOpen(s.path));

// ─── Metrics ─────────────────────────────────────────────────────────────────
const callsUploaded = new Counter('calls_uploaded');
const zipsUploaded = new Counter('zips_uploaded');
const zipsFailed = new Counter('zips_failed'); // read live by run_e2e.py for the step-test stop
const uploadOk = new Rate('upload_success');
const uploadSeconds = new Trend('upload_seconds');
const browseOk = new Rate('browse_success');

// ─── Scenario wiring: calls/min → zips/min ───────────────────────────────────
// Rates are whole zips per UNIT_MIN minutes (1 by default; 60 for calls-per-hour targets).
const UNIT_MIN = Math.max(1, parseInt(PLAN.unitMinutes || 1, 10));
const zipsPerMin = (cpm) => Math.max(0, Math.round((cpm * UNIT_MIN) / CALLS_PER_ZIP));
const peakZips = Math.max(1, ...PLAN.stages.map((s) => zipsPerMin(s.callsPerMin)));
const peakZipsPerS = peakZips / (UNIT_MIN * 60);
const totalMin = PLAN.stages.reduce((a, s) => a + s.minutes, 0);

// Burst mode: every zip of the burst is sent at once (one VU per zip, capped), no pacing.
const BURST_ZIPS = PLAN.burst ? Math.max(1, parseInt(PLAN.burst.zips, 10)) : 0;
// Browse window for bursts. run_e2e.py passes a long cap and stops k6 once the burst is processed.
const BURST_BROWSE_S = parseInt(env('BURST_BROWSE_S', '300'), 10);
// Optional, non-standard: start zip k after k × stagger seconds instead of all at once (run_burst.py --stagger-s).
const BURST_STAGGER_S = PLAN.burst ? Math.max(0, Number(PLAN.burst.stagger_s || 0)) : 0;

const scenarios = BURST_ZIPS ? {
  upload: {
    executor: 'shared-iterations',
    exec: 'uploadZip',
    vus: Math.min(BURST_ZIPS, parseInt(env('BURST_MAX_VUS', '80'), 10)),
    iterations: BURST_ZIPS,
    maxDuration: '45m',
    gracefulStop: '2m',
  },
} : {
  upload: {
    executor: 'ramping-arrival-rate',
    exec: 'uploadZip',
    timeUnit: `${UNIT_MIN}m`,
    startRate: zipsPerMin(PLAN.startCallsPerMin || 0),
    // Little's law: zips/s × ~10 s typical upload, plus headroom; maxVUs covers ~60 s uploads.
    preAllocatedVUs: Math.max(5, Math.ceil(peakZipsPerS * 10)),
    maxVUs: Math.max(20, Math.ceil(peakZipsPerS * 60)),
    stages: PLAN.stages.map((s) => ({ target: zipsPerMin(s.callsPerMin), duration: `${Math.round(s.minutes * 60)}s` })),
    gracefulStop: '2m',
  },
};
if (BROWSE_VUS > 0) {
  scenarios.browse = {
    executor: 'constant-vus',
    exec: 'browse',
    vus: BROWSE_VUS,
    duration: BURST_ZIPS ? `${BURST_BROWSE_S}s` : `${Math.ceil(totalMin * 60)}s`,
  };
}

export const options = {
  scenarios,
  thresholds: {
    // Stop a runaway test if uploads mostly fail (auth lost, endpoint down).
    upload_success: [{ threshold: 'rate>0.80', abortOnFail: true, delayAbortEval: '2m' }],
    'http_req_duration{scenario:browse}': ['p(95)<1000'],
    'http_req_failed{scenario:browse}': ['rate<0.01'],
    'http_req_duration{scenario:upload}': ['p(95)<30000'],
    dropped_iterations: ['count<5'],
  },
  summaryTrendStats: ['min', 'avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// ─── Per-VU session (refresh tokens aren't revoked on use, so each VU refreshes alone) ─
const session = { token: ACCESS_TOKEN, refresh: REFRESH_TOKEN, at: Date.now() };

function refresh() {
  if (!session.refresh) return false;
  const res = http.post(`${BASE_URL}/auth/refresh`, null, {
    cookies: { refresh_token: { value: session.refresh, replace: true } },
    tags: { name: 'POST /auth/refresh' },
  });
  if (res.status !== 200) {
    console.error(`VU ${__VU}: token refresh failed HTTP ${res.status}`);
    return false;
  }
  try {
    session.token = res.json('data.access_token');
    const c = res.cookies.refresh_token;
    if (c && c.length) session.refresh = c[0].value;
    session.at = Date.now();
    return true;
  } catch (e) {
    console.error(`VU ${__VU}: bad refresh response: ${e}`);
    return false;
  }
}

function authed(name, extra = {}) {
  if (Date.now() - session.at > TOKEN_REFRESH_MIN * 60000) refresh();
  return { headers: { Authorization: `Bearer ${session.token}` }, tags: { name }, ...extra };
}

// ─── STORED zip builder (no compression; CRCs come from the manifest) ────────
async function readSource(i) {
  const f = FILES[i];
  const out = new Uint8Array(SOURCES[i].size);
  await f.seek(0, SeekMode.Start);
  let off = 0;
  while (off < out.length) {
    const chunk = new Uint8Array(Math.min(4 * 1024 * 1024, out.length - off));
    const n = await f.read(chunk);
    if (n === null || n === 0) break;
    out.set(chunk.subarray(0, n), off);
    off += n;
  }
  if (off !== out.length) throw new Error(`short read ${SOURCES[i].path}: ${off}/${out.length}`);
  return out;
}

function ascii(s) {
  const b = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i) & 0x7f;
  return b;
}

async function buildZip(entries) {
  // entries: [{name, idx}] → ArrayBuffer of a valid PKZIP (method 0, no data descriptors)
  const parts = [];
  for (const e of entries) parts.push({ name: ascii(e.name), data: await readSource(e.idx), src: SOURCES[e.idx] });
  const localLen = parts.reduce((a, p) => a + 30 + p.name.length + p.data.length, 0);
  const centralLen = parts.reduce((a, p) => a + 46 + p.name.length, 0);
  const buf = new Uint8Array(localLen + centralLen + 22);
  const dv = new DataView(buf.buffer);
  const DOS_TIME = 0, DOS_DATE = (46 << 9) | (1 << 5) | 1; // 2026-01-01
  let o = 0;
  const offsets = [];
  for (const p of parts) {
    offsets.push(o);
    dv.setUint32(o, 0x04034b50, true); dv.setUint16(o + 4, 20, true); dv.setUint16(o + 6, 0, true);
    dv.setUint16(o + 8, 0, true); dv.setUint16(o + 10, DOS_TIME, true); dv.setUint16(o + 12, DOS_DATE, true);
    dv.setUint32(o + 14, p.src.crc32 >>> 0, true); dv.setUint32(o + 18, p.data.length, true); dv.setUint32(o + 22, p.data.length, true);
    dv.setUint16(o + 26, p.name.length, true); dv.setUint16(o + 28, 0, true);
    buf.set(p.name, o + 30); buf.set(p.data, o + 30 + p.name.length);
    o += 30 + p.name.length + p.data.length;
  }
  const cdStart = o;
  parts.forEach((p, i) => {
    dv.setUint32(o, 0x02014b50, true); dv.setUint16(o + 4, 20, true); dv.setUint16(o + 6, 20, true);
    dv.setUint16(o + 8, 0, true); dv.setUint16(o + 10, 0, true); dv.setUint16(o + 12, DOS_TIME, true); dv.setUint16(o + 14, DOS_DATE, true);
    dv.setUint32(o + 16, p.src.crc32 >>> 0, true); dv.setUint32(o + 20, p.data.length, true); dv.setUint32(o + 24, p.data.length, true);
    dv.setUint16(o + 28, p.name.length, true); dv.setUint16(o + 30, 0, true); dv.setUint16(o + 32, 0, true);
    dv.setUint16(o + 34, 0, true); dv.setUint16(o + 36, 0, true); dv.setUint32(o + 38, 0, true); dv.setUint32(o + 42, offsets[i], true);
    buf.set(p.name, o + 46);
    o += 46 + p.name.length;
  });
  dv.setUint32(o, 0x06054b50, true); dv.setUint16(o + 4, 0, true); dv.setUint16(o + 6, 0, true);
  dv.setUint16(o + 8, parts.length, true); dv.setUint16(o + 10, parts.length, true);
  dv.setUint32(o + 12, o - cdStart, true); dv.setUint32(o + 16, cdStart, true); dv.setUint16(o + 20, 0, true);
  return buf.buffer;
}

// ─── Scenario: upload one zip of CALLS_PER_ZIP calls ─────────────────────────
export async function uploadZip() {
  const it = exec.scenario.iterationInTest; // unique across VUs → unique filenames
  if (BURST_STAGGER_S > 0) sleep(it * BURST_STAGGER_S); // one VU per zip: zip k starts at k × stagger
  const entries = [];
  for (let j = 0; j < CALLS_PER_ZIP; j++) {
    const idx = (it * CALLS_PER_ZIP + j) % SOURCES.length;
    const ext = SOURCES[idx].path.split('.').pop().toLowerCase();
    entries.push({ name: `${RUN_ID}_${String(it).padStart(6, '0')}_${String(j).padStart(2, '0')}.${ext}`, idx });
  }
  const zipName = `${RUN_ID}_${String(it).padStart(6, '0')}.zip`;
  const body = await buildZip(entries);

  const form = { file: http.file(body, zipName, 'application/zip'), group_id: GROUP_ID };
  if (PROGRAM_ID) form.program_id = PROGRAM_ID;
  if (CLIENT_ID) form.client_id = CLIENT_ID;
  if (DEPARTMENT_ID) form.department_id = DEPARTMENT_ID;
  if (AGENT_IDS) form.agent_ids = AGENT_IDS;

  const t0 = Date.now();
  let res = http.post(`${BASE_URL}/calls/upload-zip`, form, authed('POST /calls/upload-zip', { timeout: env('UPLOAD_TIMEOUT', '15m') }));
  if (res.status === 401 && refresh()) {
    res = http.post(`${BASE_URL}/calls/upload-zip`, form, authed('POST /calls/upload-zip', { timeout: env('UPLOAD_TIMEOUT', '15m') }));
  }
  uploadSeconds.add((Date.now() - t0) / 1000);
  const ok = check(res, { 'upload-zip 200': (r) => r.status === 200 });
  uploadOk.add(ok);
  if (ok) {
    zipsUploaded.add(1);
    callsUploaded.add(CALLS_PER_ZIP);
  } else {
    zipsFailed.add(1);
    console.warn(`upload ${zipName} failed: HTTP ${res.status} ${String(res.body || res.error || '').slice(0, 200)}`);
  }
}

// ─── Scenario: normal users on the site while the pipeline runs ──────────────
export function browse() {
  const p = (name) => authed(name, { timeout: '60s' });
  const scope = `group_ids=${GROUP_ID}&page=1&page_size=10&sort_by=date:desc`;
  const r1 = http.get(`${BASE_URL}/qa/calls?${scope}&tab=evaluated_calls`, p('GET /qa/calls (evaluated page)'));
  browseOk.add(check(r1, { 'calls list 200': (r) => r.status === 200 }));
  sleep(1 + Math.random() * 2);
  const r2 = http.get(`${BASE_URL}/qa/calls?${scope}&tab=call_under_review`, p('GET /qa/calls (pending page)'));
  browseOk.add(check(r2, { 'pending list 200': (r) => r.status === 200 }));
  const r3 = http.get(`${BASE_URL}/health/`, { tags: { name: 'GET /health' }, timeout: '30s' });
  browseOk.add(r3.status === 200);
  if (r1.status === 401 || r2.status === 401) refresh();
  sleep(2 + Math.random() * 3);
}

export function handleSummary(data) {
  const m = data.metrics;
  const v = (n, s) => (m[n] && m[n].values ? m[n].values[s] : undefined);
  const line = `k6 done: ${v('zips_uploaded', 'count') || 0} zips / ${v('calls_uploaded', 'count') || 0} calls uploaded, ` +
    `upload success ${((v('upload_success', 'rate') || 0) * 100).toFixed(2)}%, dropped iterations ${v('dropped_iterations', 'count') || 0}\n`;
  return { stdout: line, [SUMMARY_PATH]: JSON.stringify(data) };
}

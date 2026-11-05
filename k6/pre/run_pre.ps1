<#
.SYNOPSIS
  Runs the QAT performance tests against PRE (pre.bpobox.ai) in a separate window, with a log in pre\results.

.DESCRIPTION
  PRE files live in perf\k6\pre (config pre\perf.env, results pre\results); QAT keeps perf\k6\qat\perf.env and
  perf\k6\qat\results. Both use the same shared test scripts in perf\k6, recordings and safety stops.
  Heavier tests start only after 'check' has shown that PRE analyses calls at Lambda speed.
  Every run asks for the OTP and a final confirmation in its own window.

.EXAMPLE
  .\pre\run_pre.ps1 dry                           # 10 calls in one zip: login, IDs, upload and tracking work end to end
  .\pre\run_pre.ps1 dry -CallsPerZip 1            # same 10 calls as 10 single-call zips: every call dated today
  .\pre\run_pre.ps1 check                         # 100 calls: login, group, and Lambda-speed check
  .\pre\run_pre.ps1 burst -Sizes 100,200,500,1000 # bursts sent all at once (zips of 100)
  .\pre\run_pre.ps1 step                          # 200 -> 300 -> 450 -> 667 calls/min, stops at the first failing step
  .\pre\run_pre.ps1 target                        # 40,000 calls spread over 60 minutes
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('dry', 'check', 'burst', 'step', 'target')]
    [string]$Test,
    [string]$Sizes = '100,200,500,1000',
    [int]$PerHour = 40000,
    [int]$Minutes = 60,
    # Calls per zip for dry/burst. The backend dates each call by its position in its zip (1st = today,
    # 2nd = yesterday, ...), so 1 puts every call on today's date. Keep zips at once <= 10 (DB pool limit).
    [int]$CallsPerZip = 0,
    [string]$EnvFile = ''
)
$ErrorActionPreference = 'Stop'
# PowerShell 5.1 leaves $PSScriptRoot empty inside param defaults, so resolve paths here
$preDir = $PSScriptRoot                   # perf\k6\pre: PRE config and results
$k6Dir = Split-Path -Parent $preDir       # perf\k6: shared test scripts
if (-not $EnvFile) { $EnvFile = Join-Path $preDir 'perf.env' }
$EnvFile = (Resolve-Path -LiteralPath $EnvFile -ErrorAction SilentlyContinue).Path

function Fail([string]$msg) { Write-Host "STOPPED: $msg" -ForegroundColor Red; exit 1 }

# ── Config ──
if (-not $EnvFile) { Fail "Config not found (expected $(Join-Path $preDir 'perf.env'))" }
$cfg = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') {
        $cfg[$Matches[1]] = ($Matches[2] -replace '(^|\s)#.*$', '').Trim().Trim('"').Trim("'")
    }
}
foreach ($k in 'BASE_URL', 'PERF_EMAIL', 'GROUP_ID', 'SOURCE_DIR') {
    if (-not $cfg[$k]) { Fail "$k is empty in $EnvFile (PRE has its own users and groups: fill in the PRE values)" }
}

# Guard: this launcher only ever targets PRE, never QAT or production
try { $apiHost = ([uri]$cfg['BASE_URL']).Host } catch { Fail "BASE_URL is not a valid URL: $($cfg['BASE_URL'])" }
if ($apiHost -ne 'pre.bpobox.ai') { Fail "BASE_URL points at '$apiHost', not pre.bpobox.ai. QAT runs use perf\k6\qat\perf.env (the default of run_e2e.py / run_burst.py)." }

# Guard: PRE results must not land in the QAT results folder
$relResults = $(if ($cfg['RESULTS_DIR']) { $cfg['RESULTS_DIR'] } else { 'pre/results' })
$resultsDir = [IO.Path]::GetFullPath((Join-Path $k6Dir $relResults))
if (-not $resultsDir.StartsWith([IO.Path]::GetFullPath($preDir).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Fail "RESULTS_DIR resolves to $resultsDir, outside $preDir. Keep PRE results under pre\ (RESULTS_DIR=pre/results)."
}

# ── Tools and recordings ──
if (-not (Get-Command k6 -ErrorAction SilentlyContinue)) { Fail 'k6 is not installed or not on PATH.' }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 'python is not installed or not on PATH.' }
if (-not (Test-Path -LiteralPath $cfg['SOURCE_DIR'])) { Fail "SOURCE_DIR not found: $($cfg['SOURCE_DIR'])" }

# ── PRE must be up before adding load ──
$health = $cfg['BASE_URL'].TrimEnd('/') + '/health/'
try {
    $r = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 20
    Write-Host "PRE health: HTTP $($r.StatusCode)"
} catch {
    Fail "PRE health check failed ($health): $($_.Exception.Message)"
}
New-Item -ItemType Directory -Force -Path $resultsDir | Out-Null

# ── Heavier tests need a passing Lambda check first (dry and check are the small tests that come before it) ──
if ($Test -notin @('dry', 'check')) {
    & python -I -X utf8 (Join-Path $preDir 'pre_check.py') --env-file $EnvFile
    if ($LASTEXITCODE -ne 0) { Fail "Run '.\pre\run_pre.ps1 check' and get an OK before running '$Test'." }
}

# ── Command for the chosen test (no --yes: the window asks for confirmation before sending load) ──
$envArg = "--env-file '$EnvFile'"
if ($CallsPerZip -and ($CallsPerZip -lt 1 -or $CallsPerZip -gt 500)) { Fail "-CallsPerZip must be 1..500 (got $CallsPerZip)" }
if ($CallsPerZip -and $Test -notin @('dry', 'burst')) { Fail "-CallsPerZip applies to dry and burst only" }
$cpzArg = $(if ($CallsPerZip) { "--calls-per-zip $CallsPerZip" } else { '' })
switch ($Test) {
    'dry'    {
        $dryCpz = $(if ($CallsPerZip) { $CallsPerZip } else { 10 })
        $py = "run_burst.py --sizes 10 --calls-per-zip $dryCpz $envArg"
    }
    'check'  { $py = "run_e2e.py smoke $envArg" }
    'burst'  {
        if ($Sizes -notmatch '^\d+(,\d+)*$') { Fail "-Sizes must look like 100,200,500 (got '$Sizes')" }
        if ($CallsPerZip) {
            # Many zips unpacking at once exhausted QAT's DB pool (40 zips failed, <= 10 passed)
            $maxZips = ($Sizes -split ',' | ForEach-Object { [math]::Ceiling([int]$_ / $CallsPerZip) } | Measure-Object -Maximum).Maximum
            if ($maxZips -gt 10) { Fail "-CallsPerZip $CallsPerZip makes $maxZips zips at once for the largest burst; keep it to 10 or fewer." }
        }
        $py = "run_burst.py --sizes $Sizes $cpzArg $envArg"
    }
    'step'   { $py = "run_e2e.py step $envArg" }
    'target' { $py = "run_e2e.py target --per-hour $PerHour --minutes $Minutes $envArg" }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = Join-Path $resultsDir "pre_${Test}_console_$stamp.log"
# The shared scripts run from perf\k6; the env file sends their output to pre\results
$cmd = "Set-Location -LiteralPath '$k6Dir'; `$host.UI.RawUI.WindowTitle = 'BPO-BOX PRE - $Test'; " +
       "python -I -u -X utf8 $py 2>&1 | Tee-Object -FilePath '$log'"
$p = Start-Process powershell -ArgumentList '-NoExit', '-Command', $cmd -PassThru

Write-Host ''
Write-Host "Started '$Test' on PRE in a new window (PID $($p.Id))." -ForegroundColor Green
Write-Host 'Enter the OTP and confirm in that window.'
Write-Host "Log    : $log"
Write-Host "Results: $resultsDir"
if ($Test -eq 'dry') {
    Write-Host ''
    Write-Host "Type 'burst' in that window to confirm. On Lambda the 10 calls should be analysed within a few minutes;" -ForegroundColor Yellow
    Write-Host 'if nothing is analysed after ~10 minutes, PRE is likely on Celery.'
}
if ($Test -eq 'check') {
    Write-Host ''
    Write-Host 'When it finishes, run:  python -I pre\pre_check.py' -ForegroundColor Yellow
    Write-Host 'If fewer than ~20 calls are analysed after 5 minutes, PRE is likely on Celery: press Ctrl+C in the window and run pre_check.py.'
}

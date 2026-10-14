<#
.SYNOPSIS
  One command from a fresh clone to a generated Playwright test for the seeded ticket.

.DESCRIPTION
  1. finds Python 3.11+, creates .venv and installs requirements (only when they changed)
  2. checks .env (the one you were sent; falls back to .env.example)
  3. runs the seeded demo ticket (TB-601, TICKET_SOURCE=seed) through the pipeline
  4. writes the generated spec into YOUR local tripbuilder-e2e clone (e2e/qa/<TICKET>.spec.ts)
     and runs it - nothing is committed or pushed unless you pass -Push

  The model comes from LLM_PROVIDER in .env (auto = Claude when ANTHROPIC_API_KEY is
  set, otherwise Ollama). -Provider offline proves the pipeline with no model, but
  then no spec is written.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run-seeded.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run-seeded.ps1 -Provider claude -TestRepo C:\code\tripbuilder-e2e
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run-seeded.ps1 -Push   # agent clone + push qa/TB-601
#>
param(
  [string]$Ticket = "TB-601",
  [ValidateSet("triage", "architect", "automation", "full")]
  [string]$Stage = "full",
  [ValidateSet("", "claude", "ollama", "auto", "offline")]
  [string]$Provider = "",
  # Your clone of github.com/regina-triparc/tripbuilder-e2e. Relative = from this repo's root.
  [string]$TestRepo = "..\tripbuilder-e2e",
  # Use the managed agent clone from .env and push a qa/<TICKET> branch instead of writing locally.
  [switch]$Push
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n[FAIL] $text" -ForegroundColor Red; exit 1 }

# ---- 1. Python + virtualenv -------------------------------------------------
Step "Python environment"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  $candidates = @(@("py", "-3.12"), @("py", "-3.11"), @("python"))
  $base = $null
  foreach ($c in $candidates) {
    try {
      $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
      $ok = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
      if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq "True") { $base = $c; break }
    } catch { }
  }
  if ($null -eq $base) { Fail "Python 3.11+ not found. Install it from python.org (tick 'py launcher'), then rerun." }
  Write-Host "creating .venv with $($base -join ' ')"
  $exe = $base[0]; $rest = @($base | Select-Object -Skip 1)
  & $exe @rest -m venv .venv
  if ($LASTEXITCODE -ne 0) { Fail "could not create .venv" }
}
$hash = (Get-FileHash requirements.txt -Algorithm SHA256).Hash
$stamp = Join-Path $root ".venv\.requirements.sha256"
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $hash) {
  Write-Host "installing requirements (first run takes a few minutes)"
  & $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt
  if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed. If the error mentions a missing file, clone this repo to a short path such as C:\code\TB-TAF (Windows 260-character path limit)."
  }
  Set-Content -Path $stamp -Value $hash -Encoding ascii
} else { Write-Host "requirements already installed" }

# ---- 2. .env ----------------------------------------------------------------
Step ".env"
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "[warn] no .env - copied .env.example. Put the values you were sent into .env for a real model." -ForegroundColor Yellow
} else { Write-Host ".env present" }

# ---- 3. Where the generated test goes ---------------------------------------
Step "Test repository"
$repoPath = $TestRepo
if (-not [System.IO.Path]::IsPathRooted($repoPath)) { $repoPath = Join-Path $root $repoPath }
$haveRepo = Test-Path (Join-Path $repoPath "playwright.config.ts")
if ($Push) {
  Write-Host "managed mode from .env: the agent's own clone, branch qa/$Ticket is committed and pushed"
} elseif ($haveRepo) {
  $repoPath = (Resolve-Path $repoPath).Path
  Write-Host "local mode: spec is written to $repoPath\e2e\qa\$Ticket.spec.ts (not committed, not pushed)"
  $env:TEST_REPO_LOCAL_PATH = $repoPath
  $env:TEST_REPO_NO_GIT = "true"
  $env:GIT_PUSH_ENABLED = "false"
  $loginFile = Join-Path $repoPath ".env"
  if (Test-Path $loginFile) { $env:TEST_REPO_ENV_FILE = $loginFile }
  else { Write-Host "[warn] $loginFile missing - add TBA_E2E_EMAIL / TBA_E2E_PASSWORD there or the spec cannot sign in" -ForegroundColor Yellow }
  if ($Stage -in @("automation", "full")) {
    Push-Location $repoPath
    try {
      if (-not (Test-Path "node_modules")) { Write-Host "npm ci in the test repo"; npm ci --no-audit --no-fund }
      npx playwright install chromium | Out-Null
    } finally { Pop-Location }
  }
} else {
  Write-Host "[warn] no tripbuilder-e2e clone at $repoPath - stopping after the test plan." -ForegroundColor Yellow
  Write-Host "       To get tests generated: git clone https://github.com/regina-triparc/tripbuilder-e2e.git `"$repoPath`""
  if ($Stage -in @("automation", "full")) { $Stage = "architect" }
}

# ---- 4. Check, then run -----------------------------------------------------
Step "Setup check"
& $venvPy -m taf doctor
if ($LASTEXITCODE -ne 0 -and $Provider -ne "offline") {
  Fail "setup check failed (see above). Fix it, or prove the pipeline without a model: scripts\run-seeded.ps1 -Provider offline"
}

Step "Running $Ticket (seed) - stage $Stage"
$spec = Join-Path $repoPath "e2e\qa\$Ticket.spec.ts"
$before = if (Test-Path $spec) { (Get-Item $spec).LastWriteTimeUtc } else { [datetime]::MinValue }
$runArgs = @("-m", "taf", "run", $Ticket, "--source", "seed", "--stage", $Stage)
if ($Provider) { $runArgs += @("--provider", $Provider) }
& $venvPy @runArgs
$code = $LASTEXITCODE

Step "Result"
$written = (Test-Path $spec) -and ((Get-Item $spec).LastWriteTimeUtc -gt $before)
if (-not $Push -and $haveRepo -and $written) {
  Write-Host "spec written: $spec"
  Write-Host "run it again:  cd `"$repoPath`"; npx playwright test e2e/qa/$Ticket.spec.ts --project=qa-agent --reporter=list"
  Write-Host "discard it:    cd `"$repoPath`"; git checkout -- e2e/qa/$Ticket.spec.ts"
}
if (-not $Push -and $haveRepo -and -not $written) {
  Write-Host "no spec was written this run (offline provider, or the automation stage did not get that far - see above)"
}
if ($Push) { Write-Host "branch: https://github.com/regina-triparc/tripbuilder-e2e/tree/qa/$Ticket" }
Write-Host "dashboard:     .venv\Scripts\python.exe -m taf serve   ->  http://127.0.0.1:8000/dashboard"
exit $code

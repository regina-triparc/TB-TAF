# Shared setup for scripts\run-seeded.ps1 and scripts\start-dashboard.ps1. Dot-source it.
#
# Expects from the caller: $Provider, $TestRepo, $Push (switch), $NeedsTestRepo (bool),
# $ScriptName. Sets: $root, $venvPy, $repoPath, $haveRepo, $doctorOk, and - for the
# process and everything it starts - the env overrides that make generated specs go to
# the local tripbuilder-e2e clone without committing or pushing.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # this file's folder is scripts/lib
Set-Location $root
function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "[warn] $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "`n[FAIL] $text" -ForegroundColor Red; exit 1 }

# ---- Python + virtualenv ----------------------------------------------------
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

# ---- .env ---------------------------------------------------------------------
Step ".env"
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Warn "no .env - copied .env.example. Put the values you were sent into .env for a real model."
} else { Write-Host ".env present" }
if ($Provider) { $env:LLM_PROVIDER = $Provider }

# ---- Where generated tests go -----------------------------------------------
Step "Test repository"
$repoPath = $TestRepo
if (-not [System.IO.Path]::IsPathRooted($repoPath)) { $repoPath = Join-Path $root $repoPath }
$haveRepo = Test-Path (Join-Path $repoPath "playwright.config.ts")
if ($Push) {
  Write-Host "push mode (.env settings): the agent's own clone; qa/<TICKET> branches are committed and pushed"
} elseif ($haveRepo) {
  $repoPath = (Resolve-Path $repoPath).Path
  Write-Host "local mode: specs are written to $repoPath\e2e\qa\<TICKET>.spec.ts (not committed, not pushed)"
  $env:TEST_REPO_LOCAL_PATH = $repoPath
  $env:TEST_REPO_NO_GIT = "true"
  $env:GIT_PUSH_ENABLED = "false"
  $loginFile = Join-Path $repoPath ".env"
  if (Test-Path $loginFile) { $env:TEST_REPO_ENV_FILE = $loginFile }
  else { Warn "$loginFile missing - add TBA_E2E_EMAIL / TBA_E2E_PASSWORD there or generated specs cannot sign in" }
  if ($NeedsTestRepo) {
    Push-Location $repoPath
    try {
      if (-not (Test-Path "node_modules")) { Write-Host "npm ci in the test repo"; npm ci --no-audit --no-fund }
      npx playwright install chromium | Out-Null
    } finally { Pop-Location }
  }
} else {
  # Never push unless asked: without a local clone, keep whatever .env says but stay local.
  $env:GIT_PUSH_ENABLED = "false"
  Warn "no tripbuilder-e2e clone at $repoPath - generated specs cannot be written locally."
  Write-Host "       Clone it first: git clone https://github.com/regina-triparc/tripbuilder-e2e.git `"$repoPath`""
}

# ---- Setup check ------------------------------------------------------------------
Step "Setup check"
& $venvPy -m taf doctor
$doctorOk = ($LASTEXITCODE -eq 0)
if (-not $doctorOk -and $Provider -ne "offline") {
  Fail "setup check failed (see above). Fix it, or prove the pipeline without a model: $ScriptName -Provider offline"
}

<#
.SYNOPSIS
  Run the same eval case(s) on two or more providers and open a visual comparison.

.DESCRIPTION
  1. runs `python -m taf eval` once per provider (default: ollama, then claude)
  2. builds an HTML report with `python -m taf eval report` and opens it

  Nothing is committed, pushed or opened as a PR, and no run history is written:
  evals are separate from QA runs. With -WithRepo the generated spec is also
  type-checked, linted and loaded (`playwright --list`) in the test repository from
  .env (TEST_REPO_LOCAL_PATH), and removed again afterwards.

  Claude costs money (TB-601: about $0.70-0.90 per run); Ollama takes several
  minutes on CPU. Results go to evals\results\ (git-ignored).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1 -Case tb-601,sample-password-reset -WithRepo
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1 -Providers offline,ollama   # free
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1 -Langfuse   # also trace into Langfuse (environment "eval")
#>
param(
  [string[]]$Case = @("tb-601"),
  [string[]]$Providers = @("ollama", "claude"),
  [ValidateSet("triage", "architect", "automation")]
  [string]$Stage = "automation",
  [switch]$WithRepo,
  [switch]$Judge,
  [switch]$Langfuse,
  [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`n[FAIL] $text" -ForegroundColor Red; exit 1 }

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "no .venv yet - run scripts\run-seeded.ps1 once (it sets up Python), then rerun." }
if (-not (Test-Path ".env")) { Fail "no .env - copy .env.example to .env and fill it in." }
$Providers = @($Providers | ForEach-Object { $_ -split "," } | Where-Object { $_ })
$Case = @($Case | ForEach-Object { $_ -split "," } | Where-Object { $_ })
if ($Providers.Count -lt 2) { Fail "give at least two providers to compare, e.g. -Providers ollama,claude" }

# Evals never publish; make that explicit for this process. The seed file keeps
# TB-601 the same text on every machine (your Jira copy may differ).
$env:GIT_PUSH_ENABLED = "false"
$env:PR_CREATE_ENABLED = "false"
$env:TICKET_SOURCE = "seed"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$results = @(); $n = 0
foreach ($p in $Providers) {
  Step "Eval on $p ($($Case -join ', ') through $Stage)"
  $n++; $out = "evals\results\compare-$stamp-$n-$p.json"
  $evalArgs = @("-m", "taf", "eval", "--provider", $p, "--stage", $Stage, "--label", $p, "--out", $out)
  foreach ($c in $Case) { $evalArgs += @("--case", $c) }
  if ($WithRepo) { $evalArgs += "--with-repo" }
  if ($Judge) { $evalArgs += "--judge" }
  if ($Langfuse) { $evalArgs += "--langfuse" }
  & $venvPy @evalArgs
  # Exit 1 = some case failed its checks: still a result worth comparing.
  if (-not (Test-Path $out)) { Fail "the $p eval produced no result file (see the output above)" }
  $results += $out
}

Step "Report"
$report = "evals\results\compare-$stamp.html"
& $venvPy -m taf eval report @results --out $report
if ($LASTEXITCODE -ne 0) { Fail "could not build the report" }
& $venvPy -m taf eval compare $results[0] $results[-1]
$full = (Resolve-Path $report).Path
Write-Host "`nreport: $full"
if (-not $NoBrowser) { Start-Process $full }

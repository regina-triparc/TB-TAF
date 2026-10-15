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
$NeedsTestRepo = $Stage -in @("automation", "full")
$ScriptName = "scripts\run-seeded.ps1"
. (Join-Path $PSScriptRoot "lib\setup.ps1")
if (-not $Push -and -not $haveRepo -and $NeedsTestRepo) {
  Warn "stopping after the test plan (no local tripbuilder-e2e to write the spec into)"
  $Stage = "architect"
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
  Write-Host "test cases:   $(Join-Path $repoPath "xray\$Ticket")\  (Xray JSON, CSV, readable plan)"
  Write-Host "run it again:  cd `"$repoPath`"; npx playwright test e2e/qa/$Ticket.spec.ts --project=qa-agent --reporter=list"
  Write-Host "discard it:    cd `"$repoPath`"; git checkout -- e2e/qa/$Ticket.spec.ts; Remove-Item -Recurse xray\$Ticket   (or git checkout -- xray/$Ticket if it was committed)"
}
if (-not $Push -and $haveRepo -and -not $written) {
  Write-Host "no spec was written this run (offline provider, or the automation stage did not get that far - see above)"
}
if ($Push) {
  Write-Host "push mode: nothing is pushed until you approve (REQUIRE_APPROVAL in .env) -"
  Write-Host "           use the resume command printed above, or Approve on the dashboard."
  Write-Host "branch after approval: https://github.com/regina-triparc/tripbuilder-e2e/tree/qa/$Ticket"
}
Write-Host "dashboard:     .venv\Scripts\python.exe -m taf serve   ->  http://127.0.0.1:8000/dashboard"
exit $code

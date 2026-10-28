<#
.SYNOPSIS
  One command: set everything up, start the QA service and open the dashboard.

.DESCRIPTION
  Same setup as run-seeded.ps1 (venv, requirements, .env, local tripbuilder-e2e,
  doctor), then starts the service and opens http://127.0.0.1:<Port>/dashboard.
  On the board, "Full run" on a ticket runs triage -> test plan -> spec -> report;
  the spec is written into your local tripbuilder-e2e clone and run there. Nothing
  is committed or pushed unless you pass -Push.

  Stop the service with Ctrl+C in this window.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start-dashboard.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start-dashboard.ps1 -Source jira -Provider claude
#>
param(
  # seed = demo queue (TB-601); jira = your real Jira queue; auto = Jira if configured.
  [ValidateSet("seed", "jira", "auto")]
  [string]$Source = "seed",
  # Startup default; the board's Model selector can switch it while running.
  [ValidateSet("", "claude", "ollama", "auto", "offline")]
  [string]$Provider = "",
  [string]$TestRepo = "..\tripbuilder-e2e",
  [switch]$Push,
  [int]$Port = 8002,
  [switch]$NoBrowser
)
$NeedsTestRepo = $true
$ScriptName = "scripts\start-dashboard.ps1"
. (Join-Path $PSScriptRoot "lib\setup.ps1")

# ---- Is the port free? --------------------------------------------------------
Step "Starting the service"
$busy = $false
try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $busy = $c.Connected; $c.Close() } catch { }
if ($busy) {
  Fail ("Port $Port is already in use - probably a QA service started earlier, which still has its old settings. " +
        "Stop it (Ctrl+C in its window), or run this script with -Port 8001.")
}

$env:TICKET_SOURCE = $Source
$env:PORT = "$Port"
$url = "http://127.0.0.1:$Port/dashboard"
Write-Host "ticket source: $Source   provider: $(if ($Provider) { $Provider } else { 'from .env' })"
if ($haveRepo -and -not $Push) { Write-Host "Full run writes specs to: $repoPath\e2e\qa\" }
Write-Host "dashboard:     $url"
Write-Host "stop with Ctrl+C`n"

if (-not $NoBrowser) {
  # Open the board once the service answers; the service itself runs in this window.
  Start-Job -ArgumentList $Port, $url -ScriptBlock {
    param($port, $url)
    for ($i = 0; $i -lt 120; $i++) {
      try { Invoke-WebRequest "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process $url; return }
      catch { Start-Sleep -Seconds 1 }
    }
  } | Out-Null
}
& $venvPy -m taf serve --port $Port

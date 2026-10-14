# Demo runbook — local QA pipeline (no Jira key required)

Everything below runs in a **PowerShell** terminal in VS Code. Jira credentials are
optional: with none set, the board reads `data/seed_tickets.json`, which already holds
TB-601. That is how every run in this project has been demonstrated so far.

> **Don't use bare `curl` in PowerShell** — it is an alias for `Invoke-WebRequest`
> and takes different arguments. Use `Invoke-RestMethod`, as below.

Paths below are the ones from the machine this demo was recorded on
(`C:\Users\r.abduraxmanova\Documents\localTaf\TAF` for this repo and
`C:\Users\r.abduraxmanova\Documents\taf-tb\TripArc.TripBuilder\apps\search-e2e` for the
Playwright project). **Substitute your own** — the second one is whatever you set
as `TEST_REPO_LOCAL_PATH` in `.env`. See the README for how to choose it.

---

## Shortcut: no server, no model

Everything below can also be done without starting the service. From the repo
root, with the virtualenv active:

```powershell
python -m taf doctor
```

```powershell
python -m taf run TB-601 --provider offline --stage full
```

```powershell
python -m taf run TB-601 --stage triage
```

The first checks the setup, the second proves the pipeline end to end with no
model, the third is a real triage on whatever `LLM_PROVIDER` resolves to. Runs
started this way show up on the dashboard too.

To switch the model of a service that is already running (instead of editing
`.env` and restarting), use the **Model** selector on the board or:

```powershell
python -m taf providers --set ollama --server http://127.0.0.1:8000
```

---

## 0. Free some memory first

The model needs ~2.4 GB resident. Below that, a 40-second call can take 30 minutes.
**Close Chrome before demoing.** Check:

```powershell
"{0} GB free" -f [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB,1)
```

Under ~2.5 GB, close browsers and anything heavy.

---

## 1. Start Ollama (tab 1)

The tray app does not reliably start the server, and `setx` variables do **not**
reach a server launched from a shell that started earlier. Set them in the same
command:

```powershell
$env:OLLAMA_CONTEXT_LENGTH='4096'; $env:OLLAMA_NUM_PARALLEL='1'; $env:OLLAMA_MAX_LOADED_MODELS='1'; $env:OLLAMA_KEEP_ALIVE='60m'; & "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
```

Leave it running. Verify in another tab:

```powershell
Invoke-RestMethod http://localhost:11434/api/version
```

---

## 2. Start the QA service (tab 2)

```powershell
cd C:\Users\r.abduraxmanova\Documents\localTaf\TAF; .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Verify (tab 3) — every agent should read `provider=ollama`:

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/health).claude_routes | ConvertTo-Json
```

---

## 3. Open the dashboard

```powershell
Start-Process "http://127.0.0.1:8000/dashboard"
```

TB-601 appears with its pipeline strip and the stage buttons. The amber banner
saying Jira is not configured is expected — the ticket is coming from the seed file.

---

## 4. Start a stage from the terminal

Equivalent to clicking a button on the board. `stage` is one of
`triage`, `architect`, `automation`, `full` — each runs the stages before it too.

```powershell
$body = @{ ticket_id = "TB-601"; stage = "triage" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/dashboard/api/start -Method Post -ContentType 'application/json' -Body $body
```

For the full generation, which writes the spec and runs Playwright:

```powershell
$body = @{ ticket_id = "TB-601"; stage = "automation"; target_url = "https://tripbuilder-portal-gmguhndwguh2fxh6.a03.azurefd.net/" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/dashboard/api/start -Method Post -ContentType 'application/json' -Body $body
```

**Timing on this laptop:** triage ~100s, architect ~175s, automation ~300s
(plus ~2 min of Playwright). Budget 10–12 minutes for a full automation run.

---

## 5. Watch progress from the terminal

```powershell
while ($true) {
  $r = (Invoke-RestMethod http://127.0.0.1:8000/dashboard/api/board).rows[0].run
  Clear-Host; "state: $($r.state)"
  $r.phases | ForEach-Object { "  {0,-11} {1,-8} {2}" -f $_.phase, $_.state, $_.detail }
  if ($r.state -ne 'running') { break }
  Start-Sleep -Seconds 10
}
```

---

## 6. Show what the agent produced

```powershell
$rid = (Invoke-RestMethod http://127.0.0.1:8000/dashboard/api/board).rows[0].run.run_id
$d = Invoke-RestMethod "http://127.0.0.1:8000/dashboard/api/run/$rid"
($d.phases | Where-Object { $_.phase -eq 'architect' }).artifact.cases |
  ForEach-Object { "  $($_.test_id) [$($_.priority)] $($_.summary)" }
```

Or click **"what the agent did"** on the board for the same thing with the
extracted criteria, the test plan and the generated spec.

---

## 7. Run the generated test directly

This is the part that proves it works end to end. Requires
`TBA_E2E_EMAIL` / `TBA_E2E_PASSWORD` in `apps/search-e2e/.env` — the `setup`
project signs in and writes `storageState`.

```powershell
cd C:\Users\r.abduraxmanova\Documents\taf-tb\TripArc.TripBuilder\apps\search-e2e; $env:CI="1"; npx playwright test e2e/qa/TB-601.spec.ts --project=qa-agent --reporter=list
```

Expected today: **TC-001 passes**, TC-002 fails (it invents edit-form selectors
that were never captured).

Open the HTML report:

```powershell
npx playwright show-report test-results/html
```

---

## Adding Jira later (optional)

Add to `.env` and restart the service; `data/seed_tickets.json` is then ignored and
the board polls Jira directly:

```
JIRA_BASE_URL=https://traveledgelab.atlassian.net
JIRA_EMAIL=<your Atlassian login>
JIRA_API_TOKEN=<id.atlassian.com - Security - API tokens>
JIRA_JQL=project = TB AND status in ("Ready for QA", "IN QA") ORDER BY updated DESC
```

---

## If something looks wrong

| Symptom | Cause / fix |
|---|---|
| A stage runs for many minutes with no progress | Free RAM below the model's ~2.4 GB. Close your browser; don't just wait |
| `Cannot reach the QA service` on the board | The uvicorn tab died. Restart step 2 |
| Nothing on port 11434 | Ollama tray app didn't start the server. Run step 1 |
| `409 already has a run in progress` | A run is still going. Cancel it on the board, or wait |
| Playwright reports `0 tests` | The spec isn't under `e2e/qa/`, or `--project=qa-agent` was omitted |
| Every generated test fails at the login page | `storageState` missing. Run `npx playwright test --project=setup` first |

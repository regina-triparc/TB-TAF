# Demo runbook

One script starts the QA service and opens its dashboard. One click on **Full
run** then takes the demo ticket TB-601 (TripBuilder's "Info services" story)
through the whole pipeline:

1. reads the ticket and the real code changes of its pull requests;
2. extracts acceptance criteria and designs a test plan;
3. writes the Xray test cases and a Playwright test into your local copy of the
   test repo;
4. runs that test against the UAT portal;
5. reports.

Nothing is committed, pushed or sent to Jira.

Commands below are PowerShell on Windows. The bash equivalents are
`scripts/start-dashboard.sh` and `scripts/run-seeded.sh`, with the same options in
`--kebab-case`.

---

## What you need

### Software

| | Version | Needed for |
|---|---|---|
| Python | 3.11+ (Windows: install from python.org with the **py launcher**) | the service |
| Node.js | 20+ | writing and running the Playwright test |
| git | any recent | cloning; reading the PR diff |
| Ollama + `qwen2.5-coder:3b` (or `:7b`) | latest | the local model. Not needed if you use Claude |

### Access and secrets

| What | Where it goes | Needed? |
|---|---|---|
| GitHub read access to `regina-triparc/TB-TAF` and `regina-triparc/tripbuilder-e2e` (both private) | your normal git sign-in | yes |
| GitHub read access to `TravelEdge/TripArc.TripBuilder` | your normal git sign-in | no: without it the run continues without the PR diff |
| UAT test login `TBA_E2E_EMAIL` / `TBA_E2E_PASSWORD` | `tripbuilder-e2e\.env` | yes: the generated test signs in with it |
| `ANTHROPIC_API_KEY` | `TB-TAF\.env` | only to use Claude instead of Ollama |
| Jira API token | `TB-TAF\.env` | no: the demo uses the seed ticket, not Jira |

### Machine

The local 3B model needs about **2.5 GB of free RAM**, and 8 GB+ is comfortable.
Below that, one model call can take an hour and time out. Claude has no such
requirement.

---

## One-time setup (about 15 minutes)

### 1. Clone both repos side by side, at a short path

Some Python dependencies have very deep file paths, and Windows' 260-character
limit breaks `pip install` under a long folder.

```powershell
mkdir C:\code; cd C:\code
```

```powershell
git clone https://github.com/regina-triparc/TB-TAF.git
```

```powershell
git clone https://github.com/regina-triparc/tripbuilder-e2e.git
```

With this layout the scripts find the test repo by themselves (`..\tripbuilder-e2e`).
Elsewhere, pass `-TestRepo <path>`.

### 2. Prepare the test repo and check the login

```powershell
cd C:\code\tripbuilder-e2e; npm ci; npx playwright install chromium
```

```powershell
copy .env.example .env
```

Put `TBA_E2E_EMAIL` and `TBA_E2E_PASSWORD` in `.env`, then check that sign-in works:

```powershell
npx playwright test --project=setup
```

It must pass. If it fails, every generated test will stop at the login page.

### 3. Prepare TB-TAF's settings

```powershell
cd C:\code\TB-TAF; copy .env.example .env
```

The defaults are right for the demo. Only choose the model:

- **Claude:** put `ANTHROPIC_API_KEY=...` in `.env` (`LLM_PROVIDER=auto` then uses it).
- **Ollama:** leave the key empty. Pull the model:

```powershell
ollama pull qwen2.5-coder:3b
```

Then set these in `.env`. They are not secret; they are the tuning the demo was run
with on a laptop:

```ini
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5-coder:3b
OLLAMA_TIMEOUT_SECONDS=1800
OLLAMA_MAX_TEST_CASES=2
SPEC_REPAIR_ATTEMPTS=1
MAX_ENV_LOG_CHARS=4000
MAX_EXEC_LOG_CHARS_FOR_LLM=8000
```

| Setting | Why |
|---|---|
| `OLLAMA_MODEL` | Must match the model you pulled, or every call fails. |
| `OLLAMA_TIMEOUT_SECONDS` | A step takes about 2 minutes on a laptop, longer with little free RAM; the default 300 s can time out. |
| `OLLAMA_MAX_TEST_CASES` | Caps the test plan at 2 cases, to keep the run short. Raise it (e.g. 5) for more coverage at the cost of time. |
| `SPEC_REPAIR_ATTEMPTS` | One retry if the spec won't load, instead of two. |
| `MAX_*_LOG_CHARS*` | Keep logs sent to the local model inside its 4k-token context. |

No secret from TB-TAF's `.env` is needed for the demo. The Jira and service API
keys are only for the real Jira flow and the optional n8n integration.

The script creates the Python environment and installs everything else on its
first run.

---

## Run the demo

### 1. Free memory, and start Ollama (Ollama only)

```powershell
"{0} GB free" -f [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB,1)
```

Under about 2.5 GB, close browsers and other heavy apps first. Then start Ollama in its own
window. The tray app does not reliably start the server, and these settings must be
set in the same window:

```powershell
$env:OLLAMA_CONTEXT_LENGTH='4096'; $env:OLLAMA_NUM_PARALLEL='1'; $env:OLLAMA_KEEP_ALIVE='60m'; & "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
```

### 2. Start the dashboard

```powershell
cd C:\code\TB-TAF; powershell -ExecutionPolicy Bypass -File scripts\start-dashboard.ps1
```

The script:
1. creates `.venv` and installs the requirements (first run only, a few minutes);
2. checks the setup (`doctor`) - stop here if it reports `[FAIL]`;
3. starts the service;
4. opens <http://127.0.0.1:8000/dashboard>.

Leave this window open; Ctrl+C stops the service.

### 3. Start the run

The board shows **TB-601**. The amber "Jira not configured" or "Demo mode" banner
is expected: the ticket comes from `data/seed_tickets.json`. The chip at the top
shows the model; the **Model** selector switches it for the next run.

Click **Full run**. The phases fill in:

| Phase | What happens | Ollama, typical laptop | Claude |
|---|---|---|---|
| (start) | fetch the PR diff of #740 and #764 with git | ~15 s | same |
| triage | criteria, impacted areas, risks | ~2 min | seconds |
| architect | test plan and Xray test cases | ~2 min | seconds |
| automation | write `xray/TB-601/` and `e2e/qa/TB-601.spec.ts`, check it loads, run it against UAT | ~4 min (Playwright ~1.5 min of it) | ~2 min |
| report | Markdown report from the logs | ~1 min | ~1 min |

Click **"what the agent did"** for the criteria, the plan, the spec and the log.

### 4. Look at what was written

```powershell
cd C:\code\tripbuilder-e2e; git status --short
```

You'll see `e2e/qa/TB-601.spec.ts` (modified) and `xray/TB-601/` (new). Open
`xray/TB-601/TB-601.test-plan.md` for the readable plan. The other two files are
what would be exported to Xray.

Run the generated test yourself:

```powershell
$env:CI="1"; npx playwright test e2e/qa/TB-601.spec.ts --project=qa-agent --reporter=list
```

Show what an Xray export *would* send. This is a dry run, and nothing is sent:

```powershell
cd C:\code\TB-TAF; $env:TEST_REPO_NO_GIT="true"; .\.venv\Scripts\python.exe -m taf xray export TB-601 --test-repo ..\tripbuilder-e2e
```

### 5. The review point

This is the step the demo is about. The output is a draft for a person to review,
not a verdict. With the local model, check in particular:

- **Test cases against known defects.** TB-601 lists "no start/end time
  validation" as a known open defect. A test plan that asserts validation should
  have that case removed in review.
- **Coverage.** With `OLLAMA_MAX_TEST_CASES=2` the plan has 2 cases for TB-601's
  ~13 criteria, by design. A higher cap gives more cases, but a 3B model's cases
  get weaker and slower as the plan grows. On Claude there is no cap, and the
  prompt asks for 6–20.
- **Selectors.** Generated selectors are only as good as the page contract
  (`skills/page-contracts/tripbuilder-tsp.md`).

### 6. Clean up

```powershell
cd C:\code\tripbuilder-e2e; git checkout -- e2e/qa/TB-601.spec.ts; Remove-Item -Recurse xray\TB-601
```

Stop the service with Ctrl+C in its window.

---

## Variations

| To show | Run |
|---|---|
| The same in a terminal, no browser | `scripts\run-seeded.ps1` - same setup, prints each phase and the outputs |
| The pipeline with no model at all (any laptop, ~1 min) | `scripts\start-dashboard.ps1 -Provider offline`. Placeholder output; nothing is written to the test repo |
| Claude instead of Ollama | `-Provider claude`, or switch the **Model** selector on the board |
| The real Jira queue | `-Source jira`, with `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` in `.env` |
| The real review flow: branch `qa/TB-601` pushed to tripbuilder-e2e | `-Push`. Uses the agent's own clone (`..\tripbuilder-e2e-agent`); open the PR on GitHub |
| Exporting reviewed test cases to Xray | after merge: `python -m taf xray export TB-601`, then `--apply` (needs Xray API keys) |

---

## If something looks wrong

| Symptom | Cause / fix |
|---|---|
| `Port 8000 is already in use` | An older service is still running, with old settings. Stop it (Ctrl+C in its window), or add `-Port 8001` |
| `doctor` reports `[FAIL] Ollama not reachable` | Ollama isn't serving. Do step 1 of "Run the demo" |
| A phase runs for many minutes with no progress | Not enough free RAM for the model. Close apps; don't just wait |
| `no tripbuilder-e2e clone at ...` | The test repo isn't next to TB-TAF. Pass `-TestRepo C:\path\to\tripbuilder-e2e` |
| `pip install failed ... No such file or directory` | The path is too long. Clone to `C:\code\TB-TAF` |
| triage says "no diff" | No read access to TripArc.TripBuilder. The demo still works, with less context |
| Every generated test fails at the login page | Wrong or missing `TBA_E2E_*` in `tripbuilder-e2e\.env`. Re-run `npx playwright test --project=setup` |
| Playwright reports `0 tests` | The spec isn't under `e2e/qa/`, or `--project=qa-agent` was left out |
| `409 already has a run in progress` | A run for that ticket is still going. Cancel it on the board, or wait |
| Running scripts is disabled on this system | Start them with `powershell -ExecutionPolicy Bypass -File ...` as shown |

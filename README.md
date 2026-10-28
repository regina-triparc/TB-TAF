# Autonomous QA Multi-Agent Framework

A FastAPI + LangChain + LangGraph service that takes a Jira ticket in **Ready for QA** and runs a QA cycle:

1. Triage the acceptance criteria and the PR diff.
2. Design a functional and non-functional test plan, as Xray test cases.
3. Write the Xray test cases and a Playwright **TypeScript** spec to the test repo
   ([tripbuilder-e2e](https://github.com/regina-triparc/tripbuilder-e2e)), commit them together on a
   `qa/<TICKET>` branch for review, and run the spec.
4. Summarise the terminal logs into a Markdown report.
5. After the pull request is merged, export the reviewed test cases to Xray in Jira.

```
Jira (ticket + linked PRs) ─▶ QA service ─▶ tripbuilder-e2e, branch qa/<TICKET>:
TripArc.TripBuilder (diff) ─┘   (dashboard          xray/<TICKET>/   test cases (JSON, CSV, plan)
                                 or CLI)            e2e/qa/<TICKET>.spec.ts
                                                       │  pull request, review, merge
                                                       ▼
                                          python -m taf xray export <TICKET> ─▶ Xray in Jira
```

Runs are started from a local dashboard or a terminal. n8n and Paperclip are
optional integrations that no local run uses; see
[Services and alternatives](#services-and-alternatives).

**Contents:**
- Get started: [Run the demo](#run-the-demo) · [Quick start](#quick-start-any-machine-no-setup)
- How it works: [How it works](#how-it-works) · [Services and alternatives](#services-and-alternatives)
- Scripts and flows: [One-command scripts](#one-command-scripts) ·
  [Real vs demo flow](#two-workflows-real-jira-and-demo-seed) · [Xray](#xray-test-cases-generated-reviewed-then-exported)
- Reference: [Terminal commands](#terminal-commands) · [Project layout](#project-layout) ·
  [LLM router](#llm-router) · [Skills](#skills) · [Evals](#evals) · [Architecture](#architecture) ·
  [Setup details](#setup-details) · [Settings](#settings) · [Running the service](#running-the-service) ·
  [Optional: n8n](#optional-n8n-integration) · [Operational notes](#operational-notes)

---

## Run the demo

The demo takes the seeded ticket **TB-601** through the whole pipeline with one
click. Its test cases and Playwright test land in your local tripbuilder-e2e,
and the test runs against UAT. Nothing is committed, pushed or sent to Jira.

**What you need:**
- Python 3.11+, Node 20+ and git;
- read access to TB-TAF and tripbuilder-e2e;
- a UAT test login in `tripbuilder-e2e\.env`;
- a model: Ollama (`qwen2.5-coder:3b`, ~2.5 GB free RAM) or an `ANTHROPIC_API_KEY`.

Optionally, read access to `TravelEdge/TripArc.TripBuilder` gives the agents the
real PR diff.

With TB-TAF and tripbuilder-e2e cloned side by side (e.g. `C:\code\TB-TAF` and
`C:\code\tripbuilder-e2e`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dashboard.ps1
```

```bash
scripts/start-dashboard.sh
```

Then click **Full run** on TB-601.

**[docs/DEMO.md](docs/DEMO.md) is the full runbook.** It covers the one-time setup,
what happens in each phase and how long it takes, where to look at the results,
what to review, how to clean up, variations (Claude, no model, real Jira, push),
and troubleshooting.

## Quick start (any machine, no setup)

This proves the pipeline runs. It needs only Python 3.11+: no API key, Ollama,
Jira, Node or test repo.

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

On Windows PowerShell:
- create the venv with `py -3.12 -m venv .venv` (plain `python` is often the
  Microsoft Store stub);
- activate it with `.\.venv\Scripts\Activate.ps1`;
- use `copy` instead of `cp` below.

Clone to a short path such as `C:\code\TB-TAF`: some dependencies have file paths
deep enough to exceed Windows' 260-character limit, and `pip install` then fails.

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

```bash
python -m taf doctor
```

```bash
python -m taf run TB-601 --provider offline --stage full
```

That runs the seeded ticket through triage → test design → spec → report. The
`offline` provider uses no model: its output is placeholder text that proves the
plumbing works, and it never writes into a test repository.

```bash
python -m unittest
```

runs the smoke tests (offline, no network, a few seconds).

---

## How it works

One run of **Full run** (dashboard) or `python -m taf run TB-601 --stage full`:

| # | Step | Code | External service it touches |
|---|---|---|---|
| 0 | **Ticket.** Summary, description and linked PRs come from the ticket source: the seed file (demo) or the Jira API (real). | `taf/integrations/jira/board.py` | Jira REST + development panel (real flow only) |
| 0 | **PR diff.** The linked PRs are fetched with git into a small bare clone, then condensed per agent: every file listed, frontend templates shown first. | `taf/integrations/github/pull_requests.py` | GitHub via git: `TravelEdge/TripArc.TripBuilder` (read) |
| 1 | **Triage:** acceptance criteria, impacted areas, risks, open questions. | `taf/agents/triage/` | the model (via the LLM router) |
| 2 | **Test Architect:** risk-based test plan, as Xray test cases (JSON + CSV). | `taf/agents/architect/` | the model |
| 3 | **Repository context:** the test repo's own conventions - Playwright projects, page objects, fixtures, helpers, an example spec - read from its base branch. | `taf/steps/context.py` | GitHub via git: `tripbuilder-e2e` (fetch) |
| 4 | **Automation Engineer:** writes the Playwright spec (text only). | `taf/agents/automation/` | the model |
| 5 | **Static validation (mandatory gate):** no Node APIs, imports exist, `playwright test --list`, `tsc --noEmit`, the repo's ESLint. A fixable failure goes back to step 4, at most `SPEC_REPAIR_ATTEMPTS` times. | `taf/steps/validation.py` | none (local) |
| 6 | **Playwright run** against the target, then a **review** (rules, plus an optional LLM review). | `taf/steps/validation.py`, `taf/steps/review.py` | the UAT portal (Playwright) |
| 7 | **Approval:** the run pauses until a person approves (dashboard or `taf resume`). | `taf/steps/publish.py` | none |
| 8 | **Publish:** commit spec + `xray/<TICKET>/` on `qa/<TICKET>`, push, open a PR (each optional, never repeated on resume). | `taf/steps/publish.py`, `taf/integrations/github/pr_api.py` | GitHub: `tripbuilder-e2e` (push, pull request) |
| 9 | **Report Synthesizer:** a Markdown report from the logs (the verdict header is built in Python, so the model cannot change it), and an HTML report: a manual checklist pre-filled from the automation run, with screenshots, JSON and gaps. | `taf/agents/reporter/` | Ollama, or the built-in fallback |
| - | **Recorded:** every phase and its output, for the dashboard. | `taf/runs/store.py` | none (local SQLite) |
| later | **Export:** after the PR is merged, `taf xray export` imports the reviewed test cases. | `taf/integrations/jira/xray_cloud.py` | Xray Cloud API |

The steps run as one checkpointed LangGraph workflow (`taf/agents/graph.py`): a
run that crashes or waits for approval resumes where it stopped, and nothing is
pushed before validation passes and a person approves. If an agent fails, the run
skips straight to the report. Every LLM call can be traced in Langfuse - see
[docs/WORKFLOW-OBSERVABILITY-EVALS.md](docs/WORKFLOW-OBSERVABILITY-EVALS.md). Every
agent gets its model from the **LLM router** (`taf/llm/router.py`): Claude,
Ollama or offline, chosen per run and per agent. Each agent is a folder with its
code, its prompt as Markdown, and a README; see [taf/agents/README.md](taf/agents/README.md).

## Services and alternatives

What each part of the system talks to, and what can replace it. **Bold** is what
the demo and the local scripts use.

| Concern | Default | Alternatives | Switch with |
|---|---|---|---|
| **Starting a run** | **Dashboard button**, `taf run`, `scripts/*` | `POST /dashboard/api/start` (HTTP); `POST /qa/runs` (n8n or any tool, optional) | - |
| **Orchestration** (which agent runs when) | **LangGraph in the service** | - (Paperclip only mirrors runs; it does not trigger agents) | - |
| **Ticket source** | **seed file** (demo) | Jira API (real flow); the request body of `POST /qa/runs` | `TICKET_SOURCE`, `--source`, `-Source` |
| **Code under test (diff)** | **git: TripArc.TripBuilder**, PRs from Jira or the seed | a diff sent in `POST /qa/runs`; no diff (`FETCH_PR_DIFFS=false`) | `SOURCE_REPO_URL`, `FETCH_PR_DIFFS` |
| **Model** | **`auto`:** Claude if `ANTHROPIC_API_KEY` is set, else Ollama | `claude`; `ollama`; `offline` (no model); per-agent hybrid (`CLOUD_AGENTS`) | `LLM_PROVIDER`, `--provider`, the dashboard's Model selector |
| **Report formatting** | **Ollama** | built-in deterministic fallback (automatic when Ollama is down or offline) | - |
| **Where tests are written** | **local tripbuilder-e2e working copy, uncommitted** (scripts) | the agent's own clone, branch `qa/<TICKET>` committed and pushed (`.env` default); any other Playwright repo | `-TestRepo`, `-Push`, `TEST_REPO_*` |
| **Running the tests** | **Playwright, `qa-agent` project, against UAT** | any target (`--target-url`, `DASHBOARD_TARGET_URL`) | - |
| **Xray** | **files in the test repo, reviewed in the PR** | `taf xray export --apply` (Xray Cloud API); manual import of the CSV; n8n callback import (optional) | `XRAY_*` |
| **Run history** | **SQLite** (`qa_runs.db`) | - | `QA_DB_PATH` |
| **Run mirror, cost ledger, budget stop** | **service log + local token caps** | Paperclip (optional) | `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY` |
| **Agent know-how** | **`skills/*/SKILL.md`** (Claude routes) | also on Ollama; off | `SKILLS_*` |
| **Real selectors of the app** | **page contract** (`skills/page-contracts/tripbuilder-tsp.md`) | none (the model guesses) | `PAGE_CONTRACT_PATH` |
| **GitHub authentication** | **your normal git sign-in** (Git Credential Manager) | fine-grained PAT | `GITHUB_TOKEN` |

---

## One-command scripts

Both scripts do the same setup first:
1. create `.venv` and install the requirements (again only when they change);
2. create `.env` from `.env.example` if it is missing;
3. point the service at your **local tripbuilder-e2e clone in in-place mode**: no
   git operations, nothing committed or pushed (unless `-Push`);
4. run `doctor`.

The shared setup is in `scripts/lib/`.

### `start-dashboard` - the board, then Full run

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dashboard.ps1
```

```bash
scripts/start-dashboard.sh
```

Starts the service and opens <http://127.0.0.1:8000/dashboard>. Stop with Ctrl+C.

| Option (PowerShell / bash) | Default | Effect |
|---|---|---|
| `-Source` / `--source` | `seed` | `seed`: the demo queue (TB-601). `jira`: your real Jira queue. `auto`: Jira if configured. |
| `-Provider` / `--provider` | from `.env` | Startup model; the board's **Model** selector switches it while running. |
| `-TestRepo` / `--test-repo` | `../tripbuilder-e2e` | Your tripbuilder-e2e clone, if it is not next to this repo. |
| `-Push` / `--push` | off | Use the agent's own clone and push `qa/<TICKET>` branches instead. |
| `-Port` / `--port` | `8000` | Refuses to start if the port is taken - usually an older service still running with old settings. |
| `-NoBrowser` / `--no-browser` | off | Don't open the browser. |

### `run-seeded` - the same in a terminal

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-seeded.ps1
```

```bash
scripts/run-seeded.sh
```

Runs **TB-601 from the seed file** through every stage, printing each phase and
the outputs. It writes `e2e/qa/TB-601.spec.ts` and `xray/TB-601/` into your local
tripbuilder-e2e and runs the test. At the end it prints where the files are and
how to discard them. Options: `-Ticket`, `-Stage`, `-Provider`, `-TestRepo`,
`-Push` (as above). Without a tripbuilder-e2e clone it stops after the test plan.
The generated test signs in with `TBA_E2E_EMAIL` / `TBA_E2E_PASSWORD` from the
test repo's own `.env`.

### `compare-models` - the same ticket on two models, side by side

```powershell
powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1                      # tb-601: ollama vs claude
powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1 -WithRepo -Judge     # + test-repo checks, LLM judge
powershell -ExecutionPolicy Bypass -File scripts\compare-models.ps1 -Providers offline,ollama -Case sample-password-reset
```

(macOS / Linux: `scripts/compare-models.sh`, options `--providers`, `--case`, `--with-repo`,
`--judge`, `--langfuse`.) Runs the eval suite once per provider, then opens an HTML
report: checks passed per dimension, time and tokens per stage, every check with its
reason, and the criteria, test plan and spec each model wrote. Nothing is committed or
pushed. Needs `.venv` (run `run-seeded` once) and, for Claude, `ANTHROPIC_API_KEY`;
a TB-601 Claude run costs about $0.70-0.90. Any two result files can also be compared
by hand: `python -m taf eval report A.json B.json`.

## Two workflows: real (Jira) and demo (seed)

| | Real flow | Demo flow |
|---|---|---|
| Ticket comes from | the Jira API (`JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN`) | `data/seed_tickets.json` (TB-601) |
| Linked PRs come from | Jira's development panel (GitHub for Jira) | the seed ticket's `pull_requests` |
| Select it with | `TICKET_SOURCE=jira`, or `--source jira` | `TICKET_SOURCE=seed`, or `--source seed` |
| Needs | a Jira API token | nothing |

`TICKET_SOURCE=auto`, the default in `.env`, uses Jira when all three Jira
variables are set, and the seed file otherwise. `--source` on `taf run` /
`taf board`, `-Source` on the scripts, or `"source"` in `POST /dashboard/api/start`
override it. So `python -m taf run TB-601 --source seed` is always the demo.

```bash
python -m taf board --source jira
```

```bash
python -m taf run TB-622 --source jira --stage architect
```

### Where the PR diff comes from

The model never connects to GitHub itself. For each run the service:

1. **finds the PRs.** Jira's development panel lists them (TB-601 → #740 and #764
   merged, #763 declined). Declined PRs are skipped. A seed ticket lists them in
   `pull_requests` instead.
2. **fetches them with git.** The code lives in `TravelEdge/TripArc.TripBuilder`
   (`SOURCE_REPO_URL`). Fetches go into a blobless bare clone at
   `SOURCE_REPO_LOCAL_PATH` (about 15 MB, created on the first run), and the diff
   is computed against each PR's base. git uses your normal GitHub sign-in, so
   there is no token to manage. **You need read access to TripArc.TripBuilder.**
   Without it the run continues without a diff, and the triage phase says so.
3. **condenses the diff per agent.** TB-601's two PRs are about 610,000 characters,
   mostly backend C#. Each agent gets a version that fits its budget:
   - every changed file listed with its line counts;
   - then full hunks for the files most useful for UI tests: frontend templates
     under `DIFF_FOCUS_PATHS` first (roles, labels, visible text), then
     components;
   - lockfiles, docs and images (`DIFF_EXCLUDE_GLOBS`) never shown.

   The budget is `MAX_DIFF_CHARS`, or the smaller `OLLAMA_MAX_DIFF_CHARS` on local
   routes.

**Local model and real diffs.** A 3B model with a 4,096-token context sees about
6,000 characters of diff: the overview plus the three most relevant templates.
That is enough to pick up real labels and button names, not to reason about the
whole change. Its plan size is capped by `OLLAMA_MAX_TEST_CASES` (5 by default;
the demo uses 2 to keep a laptop run short), so coverage of TB-601's ~13 criteria
is limited by design. For a real ticket, use Claude for triage and automation, or
at least `CLOUD_AGENTS=automation`.

## Xray test cases: generated, reviewed, then exported

Test cases go through the same review as the spec. For each ticket, the Automation
Engineer writes `xray/<TICKET>/` into the test repo, next to `e2e/qa/<TICKET>.spec.ts`:

| File | What it is |
|---|---|
| `<TICKET>.xray.json` | Xray Cloud bulk-import payload: exactly what gets exported. Edit it in review to change what is imported. |
| `<TICKET>.xray.csv` | The same cases for Xray's Test Case Importer (manual import, audit copy). |
| `<TICKET>.test-plan.md` | The plan in readable form, for reviewers. |

They are committed together with the spec in one commit on `qa/<TICKET>`. With the
local scripts (in-place mode) they are written but not committed. They are written
even when a plan has no automatable cases.

Export happens only after review, from what was merged:

```bash
python -m taf xray export TB-601
```

```bash
python -m taf xray export TB-601 --apply
```

The first is a dry run: it reads `xray/TB-601/TB-601.xray.json` from the test repo's
base branch (`main`), meaning the merged version, and lists what would be imported.
It refuses if the PR has not been merged. `--apply` imports it with
`XRAY_CLIENT_ID` / `XRAY_CLIENT_SECRET` (Xray Cloud > Settings > API Keys) and
prints the created Jira keys. Other options:
- `--ref qa/TB-601` exports an unmerged branch.
- With the test repo used in place (`TEST_REPO_NO_GIT=true`), the working-tree
  file is read.

The tests are filed under `XRAY_TEST_FOLDER` and linked to the story with
`XRAY_REQUIREMENT_LINK_TYPE`. Exporting twice creates a second set, so export once.

## Terminal commands

Run from the project root with the virtualenv active.

| Command | What it does |
|---|---|
| `python -m taf doctor` | Checks Python, `.env`, the provider (key set / Ollama reachable), the queue, the test repo and its login, Node and the page contract. Exits 1 on a blocking problem. |
| `python -m taf board [--source jira\|seed]` | Lists the queue: Jira (real) or `data/seed_tickets.json` (demo). |
| `python -m taf providers` | Shows the router: which provider and model each agent uses. |
| `python -m taf providers --set ollama --server http://127.0.0.1:8000` | Switches the provider of a **running** service. Not persisted; `LLM_PROVIDER` in `.env` is the startup default. |
| `python -m taf run TB-601` | Runs triage + architect (default `--stage architect`; no test repo needed). |
| `python -m taf run TB-601 --stage full --provider claude` | Every stage, on Claude for this run only. Stages: `triage`, `architect`, `automation`, `full`. |
| `python -m taf run TB-601 --show all` | Also prints execution logs. `--json` prints the whole final state instead. |
| `python -m taf run TB-622 --source jira` | The real flow: ticket, linked PRs and their diff from Jira and git. `--source seed` forces the demo. |
| `python -m taf run ABC-1 --summary "..." --description "..."` | A ticket that is in neither Jira nor the seed file. |
| `python -m taf resume <run_id> --approve [--by NAME]` | Approves a run waiting at the approval gate: it publishes, then reports. `--reject --note "..."` publishes nothing. |
| `python -m taf resume <run_id>` | Continues a crashed or cancelled run from its last completed step. |
| `python -m taf xray export TB-601 [--apply]` | Exports the ticket's reviewed Xray test cases from the test repo (dry run without `--apply`). |
| `python -m taf skills` | Lists skills and the agents they apply to. |
| `python -m taf eval --provider ollama [--with-repo] [--judge] [--langfuse]` | Scores agent output against `evals/cases`. |
| `python -m taf eval compare A.json B.json` | What changed between two eval runs (models, prompts, per case and dimension). |
| `python -m taf serve [--reload] [--port N]` | Starts the API and the dashboard. |
| `scripts/start-dashboard.*`, `scripts/run-seeded.*` | One-command setup and run - see [One-command scripts](#one-command-scripts). |
| `python -m unittest` | Offline smoke tests. |

To run a generated spec by hand, from your own clone of tripbuilder-e2e (after
`git fetch` and `git checkout qa/TB-601`, or straight after a local run):

```bash
npx playwright test e2e/qa/TB-601.spec.ts --project=qa-agent --reporter=list
```

## Project layout

```
main.py                        entry point kept for `uvicorn main:app`; re-exports taf.api.app
taf/                           the service, as a package
  config.py                    every setting, read once from .env; logging with secret redaction
  cli.py, __main__.py          `python -m taf ...`
  core/                        text and HTTP helpers with no project knowledge
  llm/
    router.py                  the LLM router: claude / ollama / auto / offline, per agent and per run
    offline.py                 the no-model provider
    pricing.py, budget.py      token pricing and the cloud spend guard
  agents/                      one folder per agent: agent.py + Markdown prompt(s) + README.md
    triage/                    Agent 1  criteria, impacted areas, risks           prompt.md
    architect/                 Agent 2  test plan + Xray test cases               prompt.md, local-model-limits.md
    automation/                Agent 3  spec + xray/ into the test repo, run      prompt.md, repair-prompt.md
    reporter/                  Agent 4  logs -> Markdown report + HTML checklist  prompt.md, report-template.html
    closeout/                  Agent 5  optional n8n callback (unused locally)
    base.py                    the envelope every agent runs in (events, error handling)
    schemas.py, state.py       structured outputs and the shared LangGraph state
    graph.py, prompts.py       the LangGraph wiring; the .md prompt loader
  integrations/
    jira/board.py              ticket source: Jira queue + issues + linked PRs, or the seed file
    jira/xray.py               Xray test cases (JSON, CSV, plan) as written to the test repo
    jira/xray_cloud.py         Xray Cloud import, used by `taf xray export`
    github/pull_requests.py    reads PR diffs from the source repo; condenses them per agent
    github/repository.py       the test repo: clone, branch, write, commit, push, npm, playwright
    paperclip/control_room.py  optional Paperclip mirror and budget checks
  skills.py                    loads skills/*/SKILL.md into agent prompts
  runs/
    stages.py                  runs some stages for one ticket (dashboard and CLI)
    registry.py                background runner for POST /qa/runs (optional n8n flow)
    store.py                   SQLite run history
  api/
    app.py                     /health, /qa/runs
    dashboard.py, static/      /dashboard
skills/                        Markdown know-how appended to agent prompts (Playwright, test design)
  page-contracts/              real selectors of an app under test (PAGE_CONTRACT_PATH)
evals/                         eval cases, graders and runner
data/seed_tickets.json         the demo ticket(s): TB-601 with its PRs
scripts/                       start-dashboard (board + Full run) and run-seeded (terminal), .ps1 and .sh
  lib/                         their shared setup
tests/                         offline smoke tests (python -m unittest)
docs/                          DEMO.md (demo runbook); SETUP*.md (full hosted stack incl. optional n8n/Paperclip)
```

## LLM router

`taf/llm/router.py` is the only place that talks to a model. Agents ask it for a
route and call `ROUTER.structured(...)` or `ROUTER.text(...)`; the router picks the
client, applies the budget guard and checks stop reasons.

| Provider | Triage / Architect / Automation | Report |
|---|---|---|
| `claude` | Claude, model and effort per agent (`CLAUDE_<AGENT>_*`) | Ollama, deterministic fallback if it is down |
| `ollama` | Ollama (`OLLAMA_MODEL`, or `OLLAMA_<AGENT>_MODEL`) | Ollama, fallback if down |
| `auto` | `claude` when `ANTHROPIC_API_KEY` is set, else `ollama` | as resolved |
| `offline` | built-in placeholder, no model | fallback |

Which provider a run uses, most specific first:

1. the run's own override: `--provider` on `taf run` / the scripts, or
   `"provider"` in the body of `POST /dashboard/api/start` or `POST /qa/runs`;
2. the service default: `LLM_PROVIDER` at startup, switchable while running with the
   dashboard's Model selector, `POST /dashboard/api/provider`, or
   `python -m taf providers --set ... --server ...`;
3. then `CLOUD_AGENTS` pins individual agents to Claude inside a local run - the
   hybrid where a small local model triages and Claude writes the code.

A run's provider is fixed when it starts; switching affects the next run.
`GET /health` and `python -m taf providers` show the resolved routes.

## Skills

A skill is a Markdown file of team know-how appended to an agent's system prompt:
how to choose Playwright locators, how to assert API calls, how to order test
cases by risk. Adding expertise to an agent means adding a folder under `skills/`,
not editing Python. Six ship with the repo, four of them for Playwright.

Skills apply to Claude routes; on Ollama they are off unless `SKILLS_ON_LOCAL=true`,
because a small context window fills fast. Format and settings:
[skills/README.md](skills/README.md).

## Evals

`evals/` measures whether a change - model, prompt, skill, provider - made the
agents better or worse, per dimension (requirements, traceability, scenarios,
duplicates, ambiguity, selectors, conventions, safety/scope, TypeScript,
execution). Grading is deterministic; an optional, versioned LLM judge covers only
what rules cannot. Results record the models, prompt hashes, dataset version and
git commit, and `python -m taf eval compare` diffs two runs. Evals never create
runs, branches or PRs. Details: [evals/README.md](evals/README.md).

## Observability (Langfuse)

Optional. With `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` set, every run is a
Langfuse trace (id derived from the run id), with a span per workflow step and a
generation per model call (model, tokens, latency), masked for secrets and
truncated. Without the keys - or when Langfuse is down - runs are unaffected. See
[docs/WORKFLOW-OBSERVABILITY-EVALS.md](docs/WORKFLOW-OBSERVABILITY-EVALS.md).

---

## Architecture

### Model per agent (Claude routes)

| Agent | Default engine | Why |
|---|---|---|
| 1 Intake Triager | Claude **Sonnet 5.5**, effort `medium` | Extracting criteria and classifying risk from the ticket and diff |
| 2 Test Architect | Claude **Sonnet 5.5**, effort `high` | Risk-based test design and the Xray matrix |
| 3 TS Automation Engineer | Claude **Opus 5.5**, effort `high` + local git/npm/Playwright | The hardest task: runnable Playwright code for an app it only sees through a diff, repaired from Playwright's own loader errors |
| 4 Report Synthesizer | Ollama (`OLLAMA_MODEL`, local, free) | Log parsing and formatting |
| 5 Closeout | HTTP | Optional n8n callback; not used by local runs |

Each agent's model, effort and output limit can be changed in `.env`
(`CLAUDE_<AGENT>_MODEL` / `_EFFORT` / `_MAX_TOKENS`). On `ollama` all three model
agents use `OLLAMA_MODEL`, or a per-agent `OLLAMA_<AGENT>_MODEL`.

Structured output (triage and test plan) uses Claude's native structured outputs
(`output_config.format`). LangChain's default method forces a tool call, which
Opus 5.5 rejects and Sonnet 5.5 doesn't enforce. No `temperature` is sent: these
models think adaptively, and thinking depth is controlled with `effort`.

### LangGraph pipeline

```
gather_context → intake_triager → test_architect → repo_context → ui_snapshot → generate_spec ◀──┐
                                                                       │                  │ fixable failure /
                                                                 validate_spec ───────────┤ review blocker,
                                                                       │ ok               │ attempts left
                                       run_tests → repair_from_run → review_spec ─────────┘
                                                                       │
                                                  approval_gate (pauses) → publish → report_synthesizer → closeout
```

- **Agents** (LLM): triage, architect, automation (`generate_spec`), and the
  optional LLM review. **Everything else is deterministic Python** in
  `taf/steps/`: git, validation, Playwright, publishing. No agent runs commands
  or writes files.
- **Failure states.** An aborted agent (budget, unrecoverable model error), a
  validation failure that cannot be fixed (no test repo, dependencies missing) or
  that is still failing after `SPEC_REPAIR_ATTEMPTS`, or a rejection goes straight
  to the report. A failing Playwright test is a finding, not a gate: the PR is
  opened as a draft and says so.
- **Checkpoints.** After every step the state is saved to
  `QA_CHECKPOINT_DB_PATH` (thread = run id). `taf resume` continues a paused,
  crashed or cancelled run; completed steps are not re-run, and `publish` checks
  git and GitHub before acting, so a re-entered publish makes no second commit,
  push or PR.
- **Stages.** The dashboard and CLI run the same graph; `--stage triage|architect|automation`
  stop early (`stop_after`), `full` adds approval, publish and the report.
  `closeout` runs only for `POST /qa/runs`.

**Shared state** (`QAState` in `taf/agents/state.py`):
- ticket and inputs: `ticket_id`, `summary`, `description`, `github_diff`,
  `pull_requests`, `target_url`;
- agent outputs: `triage_analysis`, `test_plan`, `test_cases`, `xray_tests_json`,
  `xray_csv_content`, `generated_typescript_code`, `execution_logs`,
  `execution_passed`, `final_report`;
- workflow: `repo_context`, `spec_attempts`, `spec_feedback`, `validation`,
  `review`, `approval`, `publish`, `trace_url`, `stop_after`;
- operational fields: `run_id`, `llm_provider`, `ticket_source`, `git_branch`,
  `xray_files`, `errors` and `aborted`.

### The automation steps in detail

The shared clone is used under a lock, one step at a time, and left clean after
every step - the files of a run exist on disk only while a step needs them:

1. **repo_context** - managed clone: `git fetch`, `checkout <base>`, `pull --ff-only`;
   `npm ci` once if `node_modules` is missing; read the conventions. In place
   (`TEST_REPO_NO_GIT=true`): no git commands at all.
2. **generate_spec** - the model writes `e2e/qa/<TICKET>.spec.ts`; every test title
   starts with `[TC-XXX]`, so results trace back to Xray.
3. **validate_spec** - static checks first (no `child_process`/`fs`/`eval`, every
   import exists); only a safe spec is written onto the base branch and loaded with
   `playwright test --list`, then `tsc --noEmit` and ESLint where the repo has them
   (diagnostics in other files are not counted). Then the files are removed again.
4. **run_tests** - `playwright test <spec> --project=qa-agent` with `BASE_URL`,
   `CI=1` and the test login from `TEST_REPO_ENV_FILE`.
5. **review_spec** - rules (fixed sleeps, `.only`, hard-coded secrets, missing
   `[TC-xxx]` tests, uncovered criteria, raw CSS locators, unused page objects) and,
   with `REVIEW_WITH_LLM=true`, a model review. Blockers trigger a repair while
   attempts remain.
6. **approval_gate** - pauses when something would leave the machine (push or PR)
   and `REQUIRE_APPROVAL=true`.
7. **publish** - `qa/<TICKET>` continues from `origin/qa/<TICKET>` when it exists
   (a re-run adds a commit to the open PR; nothing is rewritten), else from the
   base branch. Commit only the spec and `xray/<TICKET>/`; push without force;
   open the PR when `PR_CREATE_ENABLED=true`.

Guards: the test repo must not be, contain, sit inside, or share a remote with this
project; every subprocess has a timeout and runs without a shell; the offline
provider never writes to the test repo.

### Spec repair and prompt caching

The repair turn is where prompt caching pays off on Claude:
- The context the Automation Engineer receives (triage, test cases, diff) carries a
  `cache_control` breakpoint, and each repair message adds one.
- A repair request re-sends the same prefix, which Opus reads from cache at **5% of
  its input price**.
- The first request pays the 5-minute cache-write price (1.25x), so caching comes
  out ahead as soon as one repair happens.

Triage and the test plan are single calls, so caching is off for them by default
(`PROMPT_CACHE_AGENTS=automation`). Costs are computed per model from Anthropic's
prices, cache tiers included, and appear in the report header.

### Cost guard

Every Claude call goes through `TokenBudget`, which refuses the call **before** any
money is spent when:
- the run has hit `MAX_CLOUD_TOKENS_PER_RUN`, or the day has hit
  `MAX_CLOUD_TOKENS_PER_DAY`;
- with Paperclip configured: Paperclip has paused the QA agent, or its monthly
  budget is used up.

A refused call aborts the remaining model steps; the run still ends with a
`QA Failed` report that says why.

### Paperclip Control Room (optional)

With `PAPERCLIP_API_URL` and `PAPERCLIP_API_KEY` set, every run is mirrored into
Paperclip as a Paperclip **agent**. The agent and company IDs are discovered from
the key.

| QA service | Paperclip |
|---|---|
| Run starts | Issue `[TICKET] summary` created, `in_progress`, assigned to the QA agent |
| Triage finishes | Issue priority set from the risk level |
| Each agent finishes, fails or logs | A comment: the live execution feed |
| Each model call | A cost event (tokens, cache reads, `costCents`); local models are booked at $0 |
| Run finishes | Issue moved to `done` (QA Passed) or `blocked` (failed or crashed), with the report |

Paperclip **does not start or assign work.** Runs are started by the dashboard,
the CLI, the scripts or (optionally) n8n. So in Paperclip, turn **off** *Wake on
demand* and the heartbeat for the QA agent; `/health` warns if either is on.
Events are delivered in order by one background worker. If Paperclip is offline,
runs continue and events only go to the log. Without Paperclip, the same events
go to the service log.

---

## Setup details

### Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | **3.11+** (LangGraph 1.x does not support 3.9) | everything |
| Node.js | 20+ (npm ships with it) | the automation stage (writing and running Playwright tests) |
| git | 2.31+ | reading PR diffs; the test repo |
| Ollama | latest | `LLM_PROVIDER=ollama`/`auto` without a key, and report formatting (optional: there is a fallback) |

Windows: install Python from python.org with the py launcher, and clone to a short
path. [docs/DEMO.md](docs/DEMO.md) has the full Windows walk-through.

### Installation

```bash
git clone https://github.com/regina-triparc/TB-TAF.git
```

```bash
cd TB-TAF
```

Then either run a script (`scripts/start-dashboard.*`, which sets up everything),
or follow [Quick start](#quick-start-any-machine-no-setup). Every setting is
described in `.env.example` and in [Settings](#settings).

### Local model (Ollama)

```bash
ollama pull qwen2.5-coder:3b
```

```bash
ollama serve
```

Set `OLLAMA_MODEL` to the model you pulled (`.env.example` says
`qwen2.5-coder:7b`). Check the endpoint with `python -m taf doctor`. If Ollama is
unreachable, the Report Synthesizer falls back to a deterministic report and the
run still completes.

### TypeScript test repository (Playwright)

Generated tests live in their own private repository,
**[regina-triparc/tripbuilder-e2e](https://github.com/regina-triparc/tripbuilder-e2e)**,
next to the hand-written TripBuilder suite. Nothing generated is ever written
into this repo. Ask for access to tripbuilder-e2e if you will run the automation
stage.

There are two ways to use it:
- **In place** (what the scripts do): your own clone, `TEST_REPO_NO_GIT=true`.
  Files are written and left uncommitted for you.
- **Managed clone** (the `.env.example` default): the agent keeps **its own clone**
  next to this repo (`TEST_REPO_LOCAL_PATH=../tripbuilder-e2e-agent`). It clones
  it on the first run, branches `qa/<TICKET>`, commits and pushes. A person then
  opens the pull request, reviews and merges. Never work in the agent's clone
  yourself: it switches and resets branches there.

**One-time setup on a new machine:**
- **Repo access.** git pushes with your normal GitHub sign-in (Git Credential
  Manager on Windows and macOS), so `GITHUB_TOKEN` usually stays empty. If a push
  is rejected, create a fine-grained PAT with **Contents: Read and write** on
  tripbuilder-e2e only. It is sent per command as an HTTP header, never in the
  remote URL, `.git/config`, the process list or logs.
- **Test login.** Generated specs sign in to UAT through tripbuilder-e2e's `setup`
  project, which needs `TBA_E2E_EMAIL` / `TBA_E2E_PASSWORD`. Set
  `TEST_REPO_ENV_FILE` to a `.env` that holds them, e.g. `../tripbuilder-e2e/.env`;
  the scripts do this for you. They are passed to Playwright as environment
  variables, so no secrets file is copied into the agent's clone.
- `python -m taf doctor` checks both, and whether the clone exists yet.

```ini
TEST_REPO_URL=https://github.com/regina-triparc/tripbuilder-e2e.git
TEST_REPO_LOCAL_PATH=../tripbuilder-e2e-agent
TEST_REPO_NO_GIT=false
TEST_REPO_BASE_BRANCH=main
TEST_SPEC_DIR=e2e/qa
GIT_PUSH_ENABLED=true
TEST_REPO_ENV_FILE=../tripbuilder-e2e/.env
PACKAGE_MANAGER=npm
SKIP_DEPENDENCY_INSTALL=true
PLAYWRIGHT_EXTRA_ARGS=--reporter=line --project=qa-agent
```

**Other Playwright projects.** The framework is not tied to TripBuilder. Point
`TEST_REPO_URL` at any Playwright repo that has:
- `@playwright/test` and a committed lockfile, with `PACKAGE_MANAGER` matching it;
- a `TEST_SPEC_DIR` folder covered by `testDir`/`testMatch` in
  `playwright.config.ts`, or Playwright reports `0 tests`;
- a config that reads the base URL from the environment
  (`use: { baseURL: process.env.BASE_URL }`).

### Page contract (optional but recommended)

`PAGE_CONTRACT_PATH` points at a Markdown file listing the real roles, labels and
test IDs of the application under test. Without it the Automation Engineer
invents selectors that do not exist, and the generated spec fails on its first
locator. [skills/page-contracts/tripbuilder-tsp.md](skills/page-contracts/tripbuilder-tsp.md)
is the contract for the TripBuilder portal and doubles as a format example.

---

## Settings

All settings live in `.env` (copied from `.env.example`, which documents each one).
Relative paths are resolved from the project root. Environment variables override
`.env`; the scripts use that for their in-place mode.

**Model and router**

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `claude` (`auto` in `.env.example`) | `claude`, `ollama`, `auto` or `offline` - see [LLM router](#llm-router). |
| `CLOUD_AGENTS` | – | Agents (`triage`,`architect`,`automation` or `all`) kept on Claude in a local run. |
| `ANTHROPIC_API_KEY` | – | Required for Claude routes. With `auto`, an empty key means Ollama. |
| `CLAUDE_TRIAGE_MODEL` / `_EFFORT` / `_MAX_TOKENS` | `claude-sonnet-5-5` / `medium` / `16000` | Agent 1 on Claude. |
| `CLAUDE_ARCHITECT_MODEL` / `_EFFORT` / `_MAX_TOKENS` | `claude-sonnet-5-5` / `high` / `32000` | Agent 2 on Claude. |
| `CLAUDE_AUTOMATION_MODEL` / `_EFFORT` / `_MAX_TOKENS` | `claude-opus-5-5` / `high` / `64000` | Agent 3 on Claude. `MAX_TOKENS` includes thinking. |
| `CLAUDE_TIMEOUT_SECONDS` | `600` | Timeout per Claude request. |
| `CLAUDE_PRICING_JSON` | built in | Override or add per-model prices (USD per million tokens). |
| `PROMPT_CACHE_AGENTS` / `PROMPT_CACHE_TTL` | `automation` / `5m` | Which agents' context is cached; `5m` or `1h`. |
| `SPEC_REPAIR_ATTEMPTS` | `2` | How often the model may fix a spec that won't load (0–3). |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama's OpenAI-compatible endpoint. |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Local model (all agents on `ollama`, and the report). |
| `OLLAMA_TRIAGE_MODEL` / `_ARCHITECT_` / `_AUTOMATION_` | – | Per-agent local model overrides. |
| `OLLAMA_TIMEOUT_SECONDS` | `300` | Timeout per Ollama request. |
| `OLLAMA_MAX_TOKENS` / `OLLAMA_MAX_TEST_CASES` | `4096` / `5` | Output cap per local call; plan-size cap on a local model. |

**Ticket source and code under test**

| Variable | Default | Description |
|---|---|---|
| `TICKET_SOURCE` | `auto` | `jira` (real flow), `seed` (demo) or `auto` (Jira when configured). |
| `SEED_TICKETS_PATH` | `data/seed_tickets.json` | Demo tickets; ships with TB-601. |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | – | The real flow. All three are needed. |
| `JIRA_JQL` | see `.env.example` | Which tickets the board shows. |
| `JIRA_POLL_SECONDS` / `JIRA_TIMEOUT_SECONDS` | `60` / `20` | Queue cache time and request timeout. |
| `SOURCE_REPO_URL` | TripArc.TripBuilder in `.env.example` | Repo the PR diffs are read from, with git. Empty = no diffs. |
| `SOURCE_REPO_LOCAL_PATH` / `SOURCE_BASE_BRANCH` | `../tripbuilder-src.git` / `develop` | Blobless bare clone used for diffs; base branch of the PRs. |
| `FETCH_PR_DIFFS` | `true` | `false` runs without diffs. |
| `DIFF_FOCUS_PATHS` / `DIFF_EXCLUDE_GLOBS` | `apps/` / lockfiles, docs, images | How a large diff is ranked and filtered before prompting. |
| `MAX_DIFF_CHARS` / `OLLAMA_MAX_DIFF_CHARS` | `120000` / `6000` | Diff budget on Claude / on local routes. |
| `MAX_ENV_LOG_CHARS`, `MAX_EXEC_LOG_CHARS_FOR_LLM` | see `.env.example` | Other prompt size limits. |

**Test repository and Playwright**

| Variable | Default | Description |
|---|---|---|
| `TEST_REPO_URL` | tripbuilder-e2e in `.env.example` | Clone URL of the Playwright repo. |
| `TEST_REPO_LOCAL_PATH` | `../tripbuilder-e2e-agent` in `.env.example` | The agent's clone (managed), or your checkout (in place). |
| `TEST_REPO_NO_GIT` | `false` | `true` = in place: write and run, no git operations. |
| `TEST_REPO_BASE_BRANCH` | `main` | Branch `qa/<TICKET>` branches are created from, and Xray exports read. |
| `TEST_SPEC_DIR` | `tests/qa` (`e2e/qa` in `.env.example`) | Where specs are written, relative to the repo root. |
| `TEST_REPO_ENV_FILE` | – | `.env` whose variables (the UAT test login) are passed to Playwright. |
| `GIT_PUSH_ENABLED` | `true` | `false` commits without pushing. |
| `GITHUB_TOKEN` | – | Only if your git sign-in cannot push. |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` / `GIT_TIMEOUT_SECONDS` | `QA Agent` / noreply / `120` | Commit identity; git timeout. |
| `PACKAGE_MANAGER` | `npm` in `.env.example` | `npm` or `pnpm`; must match the test repo's lockfile. |
| `SKIP_DEPENDENCY_INSTALL` | `true` in `.env.example` | Install once when `node_modules` is missing, then reuse it. |
| `PLAYWRIGHT_EXTRA_ARGS` | `--reporter=line` (`--project=qa-agent` added in `.env.example`) | Extra arguments for `playwright test`. |
| `PLAYWRIGHT_TIMEOUT_SECONDS` / `PNPM_INSTALL_TIMEOUT_SECONDS` | `1800` / `600` | Test run and install timeouts. |
| `PLAYWRIGHT_INSTALL_BROWSERS` | `false` | Install Chromium before every run. |
| `PAGE_CONTRACT_PATH` | the TripBuilder contract in `.env.example` | Real roles/labels/test IDs of the app under test. |
| `DASHBOARD_TARGET_URL` | the TripBuilder UAT portal in `.env.example` | Target for runs without `--target-url`. |

**HTML run report**

| Variable | Default | Description |
|---|---|---|
| `REPORTS_DIR` | `reports` | Where each run's HTML report goes (`<TICKET>-<run>.html`), and its usage report (`usage/<TICKET>-<run>.md` / `.json`, plus `usage/usage.csv` across runs): tokens, cost, model and time per stage. |
| `REPORT_MAX_SCREENSHOTS` | `40` | Failure screenshots inlined into one report. |

**Xray**

| Variable | Default | Description |
|---|---|---|
| `XRAY_DIR` | `xray` | Folder in the test repo for each ticket's files (`xray/<TICKET>/`). |
| `XRAY_PROJECT_KEY` | ticket prefix | Jira project the tests are created in. |
| `XRAY_TEST_FOLDER` | `AI QA/{ticket_id}` | Xray test-repository folder. |
| `XRAY_REQUIREMENT_LINK_TYPE` | `Test` | Link type from each test to the story. |
| `XRAY_INCLUDE_PRIORITY` | `false` | Send priority names; only if your Jira uses Highest…Lowest. |
| `XRAY_CLIENT_ID` / `XRAY_CLIENT_SECRET` | – | Only for `taf xray export --apply`. |
| `XRAY_BASE_URL` | `https://xray.cloud.getxray.app` | Xray Cloud API. |

**Skills, service, budget and optional integrations**

| Variable | Default | Description |
|---|---|---|
| `SKILLS_ENABLED` / `SKILLS_ON_LOCAL` / `SKILLS_MAX_CHARS` / `SKILLS_DIR` | `true` / `false` / `12000` / `skills` | See [skills/README.md](skills/README.md). |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for `taf serve`. |
| `QA_DB_PATH` | `qa_runs.db` | SQLite run history. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `MAX_CLOUD_TOKENS_PER_RUN` / `MAX_CLOUD_TOKENS_PER_DAY` | `250000` / `2000000` | Hard Claude token caps (`0` = off). Cache reads don't count. |
| `PAPERCLIP_API_URL` / `PAPERCLIP_API_KEY` | – | Optional Paperclip. Empty = disabled. |
| `PAPERCLIP_PROJECT` / `PAPERCLIP_ENFORCE_BUDGET` | – / `true` | Paperclip project; refuse Claude calls while it has paused the agent. |
| `QA_SERVICE_API_KEY` | – | Only for `POST /qa/runs` (optional n8n integration): required `X-API-Key`. |
| `CALLBACK_TIMEOUT_SECONDS` | `30` | Timeout for the n8n callback. |

---

## Running the service

The scripts start it for you. To start it by hand (development mode, auto-reload):

```bash
python -m taf serve --reload
```

`uvicorn main:app --host 127.0.0.1 --port 8000 --reload` is equivalent. Keep a
**single worker**: runs share one test-repo working tree, and the `POST /qa/runs`
registry is held in memory.

```bash
python -m taf doctor
```

```bash
curl -s http://localhost:8000/health
```

`/health` shows the resolved model routes, the ticket source, the test repo and
mode, and Paperclip status. Interactive API docs are at http://localhost:8000/docs.

### Dashboard

<http://127.0.0.1:8000/dashboard> joins the ticket queue to live pipeline state:
- **Rows:** each ticket has a phase strip (triage → architect → automation →
  report) and buttons that start a stage. Stages are cumulative, so Architect
  runs triage first.
- **Detail:** "What the agent did" shows the criteria, the plan, the spec, the
  Xray files and the log of a run.
- **Model selector:** switches the provider for new runs.
- **Queue source:** shown in the chip and banner. "Demo queue (seed)" is expected
  with `-Source seed`.
- **History:** kept in SQLite (`QA_DB_PATH`), so the board survives a restart.

The dashboard is deliberately **unauthenticated**, and the service binds to
`127.0.0.1`: do not expose the port. The same over HTTP (or use `python -m taf
run`, which needs no server):

```bash
curl -s -X POST http://127.0.0.1:8000/dashboard/api/start -H 'Content-Type: application/json' -d '{"ticket_id":"TB-601","stage":"full","source":"seed","provider":"offline"}'
```

```bash
curl -s http://127.0.0.1:8000/dashboard/api/board
```

```bash
curl -s -X POST http://127.0.0.1:8000/dashboard/api/provider -H 'Content-Type: application/json' -d '{"provider":"ollama"}'
```

`provider` and `source` in the start body are optional. On Windows use
`Invoke-RestMethod`, not `curl`, which is an alias for `Invoke-WebRequest` in
PowerShell.

---

## Optional: n8n integration

**Not used by local runs.** The dashboard, `python -m taf run` and the scripts
never call n8n. This is an alternative wiring, kept working but inactive:
1. n8n catches the Jira transition and starts a run over HTTP;
2. the last agent (Closeout, `taf/agents/closeout/`) posts the verdict back;
3. n8n updates Jira and imports into Xray.

Use it if you want Jira-triggered runs on a hosted service. Note that it imports
the model's test cases directly, without the review step above.
[docs/SETUP.md](docs/SETUP.md) (macOS) and [docs/SETUP-windows.md](docs/SETUP-windows.md)
walk through that full hosted stack (Ollama, test repo, service, Paperclip, n8n,
Jira/Xray). [docs/SETUP-ollama-only.md](docs/SETUP-ollama-only.md) is its no-Claude
variant.

### `POST /qa/runs` → `202 Accepted`

Headers: `X-API-Key: <QA_SERVICE_API_KEY>`.

```json
{
  "ticket_id": "SHOP-1234",
  "summary": "Add promo-code field to checkout",
  "description": "As a shopper… Acceptance criteria: …",
  "github_diff": "diff --git a/src/Checkout.tsx b/src/Checkout.tsx …",
  "target_url": "https://staging.example.com",
  "n8n_callback_url": "https://n8n.example.com/webhook/qa-result",
  "azure_logs": "optional",
  "db_logs": "optional",
  "provider": "optional: claude | ollama | auto | offline"
}
```

Response: `{ "run_id": "…", "ticket_id": "SHOP-1234", "status": "queued", "status_url": "/qa/runs/…" }`.
A second request for a ticket that is already running returns `409`.

### `GET /qa/runs/{run_id}`

Returns the run's status (`queued`, `running`, `completed` or `crashed`), the outcome, the branch, token usage, errors and the final report.

### Callback to n8n (Closeout agent)

```json
{
  "ticket_id": "SHOP-1234",
  "status": "QA Passed",
  "report_markdown": "## QA Report - SHOP-1234 …",
  "xray_csv_content": "\"Test ID\",\"Summary\",\"Test Type\",…",
  "xray_tests_json": [
    {
      "testtype": "Manual",
      "fields": { "summary": "[SHOP-1234] TC-001 Apply valid code", "project": { "key": "SHOP" }, "description": "…", "labels": ["ai-generated", "functional"] },
      "steps": [ { "action": "Enter code", "data": "SAVE10", "result": "10% discount shown" } ],
      "update": { "issuelinks": [ { "add": { "type": { "name": "Test" }, "outwardIssue": { "key": "SHOP-1234" } } } ] },
      "xray_test_repository_folder": "AI QA/SHOP-1234"
    }
  ],
  "metadata": { "run_id": "…", "git_branch": "qa/SHOP-1234", "git_pushed": true, "cloud_tokens_used": 41234, "cloud_cost_usd": 0.2510, "cache_read_tokens": 9000, "aborted": false, "errors": [] }
}
```

- `status` is either `"QA Passed"` or `"QA Failed"`.
- `xray_tests_json` is ready to send as-is to Xray Cloud's
  `POST /api/v2/import/test/bulk`.
- Use `metadata.run_id` as an idempotency key in n8n: the callback is retried on
  connection errors, 429 and 502–504.

### n8n workflow outline

1. **Jira Trigger**: fires on an issue transition to *Ready for QA*.
2. **GitHub node**: fetches the linked PR's diff (`Accept: application/vnd.github.diff`).
3. **Azure / DB nodes**: pull recent logs.
4. **HTTP Request**: `POST http://<host>:8000/qa/runs` with the `X-API-Key` header.
5. **Webhook (callback)**: receives the result, then:
   - transitions the Jira issue according to `status`;
   - adds `report_markdown` as a comment;
   - imports `xray_tests_json` through Xray Cloud's bulk import API;
   - attaches `xray_csv_content` to the ticket as an audit copy.

The CSV has one row per step and repeats the `Test ID` on later steps. Its columns
are `Test ID, Summary, Test Type, Priority, Labels, Requirement, Preconditions,
Action, Data, Expected Result`; labels are separated by `;`.

---

## Operational notes

- Exporting a ticket's test cases twice creates a **second** set in Xray; it doesn't update the first.
- Run history and per-phase output are persisted to SQLite (`QA_DB_PATH`), so the dashboard survives a restart. The `POST /qa/runs` registry and the daily budget counter live **in memory** and reset on restart. Keep a single worker.
- One run at a time per test repo: the automation step holds a lock on it. A second run for the same ticket is refused (`409`).
- The spec is written as `<TICKET>.spec.ts`. The ticket ID is validated, which prevents path traversal and invalid branch names; every write is confined to the test repo.
- The report header (status, counts, branch, tokens) is built in Python. The local model only formats the log details, so it cannot change the verdict.
- Secrets (`ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `GITHUB_TOKEN`, `XRAY_CLIENT_SECRET`, `PAPERCLIP_API_KEY`) are redacted from logs and subprocess output.

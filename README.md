# Autonomous QA Multi-Agent Framework

A FastAPI + LangChain + LangGraph service that takes a Jira ticket in **Ready for QA** and runs a full QA cycle without a person in the loop:

1. Triage the acceptance criteria and the PR diff.
2. Design a functional and non-functional test plan, plus Xray test cases (as bulk-import JSON and as CSV).
3. Generate a Playwright **TypeScript** spec, commit it to the test repo, and run it.
4. Summarise the terminal logs into a Markdown report.
5. Send the verdict back to n8n, which updates Jira and imports the test cases into Xray.

```
Jira ──"Ready for QA"──▶ n8n ──POST /qa/runs──▶ QA service ──callback──▶ n8n ──▶ Jira status + comment
                         (diff, Azure/DB logs)       │                         └──▶ Xray bulk import (JSON)
                                                     └──REST──▶ Paperclip (issue per run, live comments, cost ledger, budget stop)
```

Everything can also be driven from a terminal and from a local dashboard, with
no n8n, Jira or API key: the repo ships a seeded ticket (TB-601) and an
`offline` model provider, so a fresh clone can run the pipeline end to end.

## Quick start (any machine, about 2 minutes)

Needs Python 3.11+. Nothing else: no API key, no Ollama, no Jira, no Node.

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1` instead.

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

That pushes the seeded ticket through triage → test design → spec generation →
report and prints what each agent produced. The `offline` provider uses no model:
its output is mechanical placeholder text that proves the plumbing works, and its
spec is never written into a test repository.

```bash
python -m unittest
```

runs the smoke tests (same offline setup, no network, a few seconds).

### A real run

Pick a model, then run the same command without `--provider offline`:

| You have | Set in `.env` | Then |
|---|---|---|
| an Anthropic API key | `ANTHROPIC_API_KEY=...` (`LLM_PROVIDER=auto` then picks Claude) | `python -m taf run TB-601` |
| Ollama | nothing (`auto` with no key means Ollama); run `ollama pull qwen2.5-coder:7b` and `ollama serve` | `python -m taf run TB-601` |

To also **execute** the generated spec, point the service at a Playwright project
on your machine (`TEST_REPO_LOCAL_PATH`, `TEST_SPEC_DIR`; see
[TypeScript test repository](#typescript-test-repository-playwright)) and use
`--stage automation` or `--stage full`.

### The dashboard

```bash
python -m taf serve
```

Open <http://127.0.0.1:8000/dashboard>. It lists the queue (Jira, or the seed
file), starts stages with a click, shows what each agent did, and has a **Model**
selector that switches the LLM provider for new runs. Runs started from the
terminal appear there too. `uvicorn main:app --host 127.0.0.1 --port 8000` still
works and is equivalent.

## Terminal commands

Run from the project root with the virtualenv active.

| Command | What it does |
|---|---|
| `python -m taf doctor` | Checks Python, `.env`, the provider (key set / Ollama reachable), the queue, the test repo, Node and the page contract. Exits 1 on a blocking problem. |
| `python -m taf board` | Lists the queue: Jira when configured, otherwise `data/seed_tickets.json`. |
| `python -m taf providers` | Shows the router: which provider and model each agent uses. |
| `python -m taf providers --set ollama --server http://127.0.0.1:8000` | Switches the provider of a **running** service. Not persisted; `LLM_PROVIDER` in `.env` is the startup default. |
| `python -m taf run TB-601` | Runs triage + architect (default `--stage architect`; no test repo needed). |
| `python -m taf run TB-601 --stage full --provider claude` | Every stage, on Claude for this run only. Stages: `triage`, `architect`, `automation`, `full`. |
| `python -m taf run TB-601 --show all` | Also prints execution logs. `--json` prints the whole final state instead. |
| `python -m taf run ABC-1 --summary "..." --description "..."` | A ticket that is in neither Jira nor the seed file. |
| `python -m taf skills` | Lists skills and the agents they apply to. |
| `python -m taf eval --provider ollama` | Scores agent output against `evals/cases`. |
| `python -m taf serve [--reload]` | Starts the API and the dashboard. |
| `python -m unittest` | Offline smoke tests. |

To run a generated spec by hand, from inside your Playwright project:

```bash
npx playwright test e2e/qa/TB-601.spec.ts --project=qa-agent --reporter=list
```

[docs/DEMO.md](docs/DEMO.md) has the PowerShell form of the whole demo, including
the HTTP calls.

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
  agents/                      one module per agent
    triage.py, architect.py, automation.py, reporter.py, closeout.py
    base.py                    the envelope every agent runs in (events, error handling)
    schemas.py, state.py       structured outputs and the shared LangGraph state
    graph.py                   the LangGraph wiring
  integrations/
    jira/board.py              Jira queue for the dashboard (falls back to the seed file)
    jira/xray.py               Xray CSV and bulk-import JSON
    github/repository.py       the Playwright test repo: git, install, playwright
    paperclip/control_room.py  Paperclip mirror and budget checks
  skills.py                    loads skills/*/SKILL.md into agent prompts
  runs/
    stages.py                  runs some stages for one ticket (dashboard and CLI)
    registry.py                the n8n flow's background runner
    store.py                   SQLite run history
  api/
    app.py                     /health, /qa/runs
    dashboard.py, static/      /dashboard
skills/                        Markdown know-how appended to agent prompts (Playwright, test design)
  page-contracts/              real selectors of an app under test (PAGE_CONTRACT_PATH)
evals/                         eval cases, graders and runner
data/seed_tickets.json         the ticket(s) used when Jira is not configured
tests/                         offline smoke tests (python -m unittest)
docs/                          setup guides (macOS, Windows, Ollama-only) and the demo runbook
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

1. the run's own override: `--provider` on `taf run`, or `"provider"` in the body of
   `POST /dashboard/api/start` or `POST /qa/runs`;
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
not editing Python. Six ship with the repo, four of them for Playwright. Skills
apply to Claude routes; on Ollama they are off unless `SKILLS_ON_LOCAL=true`,
because a small context window fills fast. Format and settings:
[skills/README.md](skills/README.md).

## Evals

`evals/` measures whether a change - model, prompt, skill, provider - made the
agents better or worse. Each case is a ticket plus expectations; the runner sends
it through the agents and grades triage, plan and spec deterministically, without
touching a test repo. `python -m taf eval --provider offline` is the free plumbing
check; run it with `claude` or `ollama` after changing a prompt or skill. Details:
[evals/README.md](evals/README.md).

## Architecture

### Hybrid LLM routing

| Node | Agent | Engine | Why |
|---|---|---|---|
| 1 | Intake Triager | Claude **Sonnet 5.5**, effort `medium` | Extracting acceptance criteria and classifying risk from the ticket and diff |
| 2 | Test Architect | Claude **Sonnet 5.5**, effort `high` | Risk-based test design and the Xray matrix |
| 3 | TS Automation Engineer | Claude **Opus 5.5**, effort `high` + local git/pnpm | The hardest task: runnable Playwright code for an app it only sees through a diff, repaired from Playwright's own loader errors |
| 4 | Report Synthesizer | Qwen 2.5-Coder 7B on Ollama (local, free) | Log parsing and formatting |
| 5 | Closeout | HTTP | Sends the n8n callback |

Each agent's model, effort and output limit can be changed in `.env` (`CLAUDE_<AGENT>_MODEL` / `_EFFORT` / `_MAX_TOKENS`). Claude Haiku 4.5 isn't used: it can be retired as soon as October 15, 2026.

Structured output (triage and test plan) uses Claude's native structured outputs (`output_config.format`). LangChain's default method forces a tool call, which Opus 5.5 rejects and Sonnet 5.5 doesn't enforce. No `temperature` is sent: these models think adaptively, and thinking depth is controlled with `effort`.

### Spec repair loop and prompt caching

After Opus writes the spec, the service runs `playwright test <spec> --list`. This loads and transpiles the file and collects its tests without starting a browser, so it only takes a few seconds. If the spec doesn't load (syntax error, bad import, no tests), the loader output goes back to Opus as a follow-up turn, up to `SPEC_REPAIR_ATTEMPTS` times. Only a spec that loads is executed. The loop only fixes specs that **won't load**; it never "fixes" tests that fail, because a failing test is a QA result.

That repair turn is where prompt caching pays off:

- The context Opus receives (triage, test cases and diff) carries a `cache_control` breakpoint, and each repair message adds one.
- A repair request re-sends the same system prompt and context, which Opus reads from cache at **5% of its input price** ($0.20 instead of $4 per million tokens).
- The first request pays the 5-minute cache-write price (1.25x). Caching comes out ahead as soon as one repair happens.

Triage and the test plan are single calls, and caches aren't shared between models. Caching those calls would only add the write premium, so it's off by default. Turn it on with `PROMPT_CACHE_AGENTS=all` if you add retries of your own.

Costs are calculated per model from Anthropic's published prices, with cache writes and reads each at their own rate. They appear in the report header, in the callback's `metadata.cloud_cost_usd`, and in Paperclip's cost ledger.

### LangGraph pipeline

```
START → intake_triager → test_architect → automation_engineer → report_synthesizer → closeout → END
              │                 │                                       ▲
              └── aborted ──────┴───────────────────────────────────────┘
```

If a cloud node aborts (budget cap hit or an unrecoverable LLM error), the graph jumps straight to the report and closeout. **n8n always gets a callback**, with `QA Failed` and the reason.

**Shared state** (`QAState` in `taf/agents/state.py`): `ticket_id`, `summary`, `description`, `github_diff`, `target_url`, `test_plan`, `generated_typescript_code`, `execution_logs`, `execution_passed`, `xray_csv_content`, `final_report`, plus `xray_tests_json`. It also carries operational fields such as `run_id`, `git_branch`, `errors` and `aborted`.

### Node 3 in detail

1. Claude writes `tests/qa/<TICKET>.spec.ts`. Every test title is prefixed with `[TC-XXX]` so results trace back to Xray.
2. The service takes a lock on the shared test-repo clone, then runs:
   `git fetch` → `git checkout <base>` → `git pull --ff-only` → `git checkout -B qa/<TICKET>` → `git add` → `git commit` → `git push -u origin qa/<TICKET>`.
   It uses `-B` rather than `-b` so that re-running the same ticket works. A rejected push is retried with `--force-with-lease`.
3. It then runs `pnpm install` and `pnpm exec playwright test <spec> --reporter=line` with `BASE_URL=<target_url>` and `CI=1`, and captures stdout and stderr.

Error handling:

- **Remote failures don't stop the run.** If fetch, pull or push fails, the problem is logged and the tests still run.
- **Local failures skip the tests.** If checkout, write or commit fails, or pnpm is missing, the run is marked failed and the reason goes into the report.
- **Nothing hangs.** Every subprocess has a timeout.

### Paperclip Control Room

The service talks to Paperclip's REST API (`<PAPERCLIP_API_URL>/api/...`) as a Paperclip **agent**, using that agent's API key. It looks up the agent and company IDs from the key, so there are no IDs to configure. Every QA run is mirrored into Paperclip:

| QA service | Paperclip |
|---|---|
| Run starts | Issue `[TICKET] summary` is created, set to `in_progress` and assigned to the QA agent (in `PAPERCLIP_PROJECT`, if set) |
| Triage finishes | The issue priority is set from the triage risk level |
| Each agent finishes, fails or logs | A comment on the issue, which gives you the live execution feed |
| Each LLM call | A cost event with the model, input tokens, cache-read tokens, output tokens and `costCents`, priced per model including cache rates. Qwen is booked at $0 |
| Run finishes | The issue moves to `done` (QA Passed) or `blocked` (QA Failed or crashed), and the full report is added as a comment |

Events are delivered in order by one background worker. If Paperclip is offline, runs continue and the events only go to the log.

### Cost guard

Every Claude call goes through `TokenBudget`, which refuses the call **before** any money is spent if any of these is true:

- The run has hit `MAX_CLOUD_TOKENS_PER_RUN`, or the day has hit `MAX_CLOUD_TOKENS_PER_DAY`.
- Paperclip has **paused** the QA agent. Paperclip pauses an agent automatically when it goes over its monthly budget.
- The agent's `spentMonthlyCents` has reached its `budgetMonthlyCents`.

When a call is refused, the run skips the remaining cloud steps and still sends n8n a `QA Failed` callback that explains why. The Paperclip checks are cached for 30 seconds. If Paperclip can't be reached, the service falls back to its own caps (fail-open), so an outage doesn't stop QA.

> **Agent settings in Paperclip:** turn **off** both *Wake on demand* and the heartbeat for the QA agent. n8n starts runs, not Paperclip. Otherwise Paperclip tries to invoke the agent every time the service assigns it an issue. `/health` warns you if either setting is still on.

> **Full walkthrough:** [docs/SETUP.md](docs/SETUP.md) (macOS) or [docs/SETUP-windows.md](docs/SETUP-windows.md) (Windows) takes you through setting up Ollama, the test repo, this service, Paperclip, n8n and Jira/Xray in order, with a check after each phase.

## Prerequisites

| Tool | Version | Install (macOS) |
|---|---|---|
| Python | **3.11+** (LangGraph 1.x does not support 3.9) | `brew install python@3.12` |
| Node.js | 20+ | `brew install node` or nvm |
| npm or pnpm | ships with Node / `corepack enable pnpm` | Must match the lockfile in your test project (`PACKAGE_MANAGER`). |
| git | 2.31+ (needs `GIT_CONFIG_COUNT` support) | `brew install git` |
| Ollama | latest | `brew install ollama` — required for `LLM_PROVIDER=ollama`, and for the report step in either mode. |

On Windows, follow [docs/SETUP-windows.md](docs/SETUP-windows.md) for the equivalents.

## Installation

```bash
git clone https://github.com/regina-triparc/TB-TAF.git
```

```bash
cd TB-TAF
```

Then follow [Quick start](#quick-start-any-machine-about-2-minutes): virtualenv,
`pip install -r requirements.txt`, `cp .env.example .env`, `python -m taf doctor`.
Every setting is described in `.env.example` and in the table below.

### Local model (Ollama)

```bash
ollama pull qwen2.5-coder:7b
```

```bash
ollama serve
```

Check that the OpenAI-compatible endpoint responds:

```bash
curl http://localhost:11434/v1/models
```

If Ollama is unreachable, the Report Synthesizer falls back to a deterministic report. The run still completes.

### TypeScript test repository (Playwright)

The agent does not create a Playwright project for you. It writes
`<TICKET>.spec.ts` into one that already exists and then runs it. Point
`TEST_REPO_LOCAL_PATH` at that project. **This is a path on your own machine —
it is not a value anyone can send you**, and it is the one setting every new
install has to fill in.

There are two modes. Pick one.

**Mode A — in place (`TEST_REPO_NO_GIT=true`).** The recommended mode, and how
this project is normally run. Point `TEST_REPO_LOCAL_PATH` at a Playwright
working copy you already have checked out. The agent performs **no git
operations at all**: no fetch, no checkout, no branch, no commit, no push. It
writes the spec file and runs Playwright, so the branch you are on and any
uncommitted work are never touched. `TEST_REPO_URL` and `GITHUB_TOKEN` stay
empty — they are not read in this mode.

```ini
TEST_REPO_LOCAL_PATH=C:/code/TripArc.TripBuilder/apps/search-e2e
TEST_REPO_NO_GIT=true
TEST_SPEC_DIR=e2e/qa
GIT_PUSH_ENABLED=false
PACKAGE_MANAGER=npm
SKIP_DEPENDENCY_INSTALL=true
PLAYWRIGHT_EXTRA_ARGS=--reporter=line --project=qa-agent
```

**Mode B — managed clone (`TEST_REPO_NO_GIT=false`).** Give `TEST_REPO_URL` and
`GITHUB_TOKEN`; the agent clones into `TEST_REPO_LOCAL_PATH` if it is missing,
branches from `TEST_REPO_BASE_BRANCH`, commits, and pushes when
`GIT_PUSH_ENABLED=true`. `TEST_REPO_LOCAL_PATH` must then be a **dedicated**
directory the agent owns — never a clone you work in, because it checks out
branches there.

Either way, the test project needs:

- `@playwright/test` in `devDependencies` and a committed lockfile. Set
  `PACKAGE_MANAGER` to match that lockfile: a project with only
  `package-lock.json` must be driven by `npm`, not `pnpm`, or the dependency tree
  is re-resolved. Set `SKIP_DEPENDENCY_INSTALL=true` when `node_modules` is
  already installed and human-managed.
- The directory named by `TEST_SPEC_DIR` (relative to the project root) must
  exist and be covered by `testDir`/`testMatch` in `playwright.config.ts`, or
  Playwright reports `0 tests`.
- A `playwright.config.ts` that reads the base URL from the environment:
  ```ts
  import { defineConfig } from '@playwright/test';
  export default defineConfig({
    testDir: './tests',
    use: { baseURL: process.env.BASE_URL, trace: 'retain-on-failure' },
    retries: process.env.CI ? 1 : 0,
  });
  ```
- Browsers installed once on the machine, or `PLAYWRIGHT_INSTALL_BROWSERS=true`
  to install them on every run.
  ```bash
  npx playwright install chromium
  ```
- If the project uses a dedicated Playwright project (for example `qa-agent`) or
  a signed-in `storageState`, put the selecting flags in `PLAYWRIGHT_EXTRA_ARGS`
  and make sure the auth setup has been run at least once.

In Mode B, `GITHUB_TOKEN` should be a fine-grained PAT scoped only to the test
repo with **Contents: Read and write**. The service injects it per command as an
HTTP auth header through git's environment config, so it never appears in the
remote URL, in `.git/config`, in the process list or in logs.

### Page contract (optional but recommended)

`PAGE_CONTRACT_PATH` points at a Markdown file listing the real roles, labels and
test IDs of the application under test. Without it the Automation Engineer
invents selectors that do not exist and the generated spec fails on its first
locator. [skills/page-contracts/tripbuilder-tsp.md](skills/page-contracts/tripbuilder-tsp.md) is the contract for
the Trip Builder portal and doubles as a format example.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `claude` (`auto` in `.env.example`) | `claude`, `ollama`, `auto` or `offline` - see [LLM router](#llm-router). |
| `CLOUD_AGENTS` | – | Comma list of agents (`triage`,`architect`,`automation`) kept on Claude even when `LLM_PROVIDER=ollama`. |
| `ANTHROPIC_API_KEY` | – | Required for Claude routes. With `auto`, an empty key means Ollama. |
| `CLAUDE_TRIAGE_MODEL` / `_EFFORT` / `_MAX_TOKENS` | `claude-sonnet-5-5` / `medium` / `16000` | Model for Agent 1 (Intake Triager). |
| `CLAUDE_ARCHITECT_MODEL` / `_EFFORT` / `_MAX_TOKENS` | `claude-sonnet-5-5` / `high` / `32000` | Model for Agent 2 (Test Architect). |
| `CLAUDE_AUTOMATION_MODEL` / `_EFFORT` / `_MAX_TOKENS` | `claude-opus-5-5` / `high` / `64000` | Model for Agent 3 (TS Automation Engineer). `MAX_TOKENS` includes thinking. |
| `CLAUDE_TIMEOUT_SECONDS` | `600` | Timeout for each Claude request. |
| `CLAUDE_PRICING_JSON` | built in | Override or add per-model prices in USD per million tokens (for contract rates or new models). |
| `PROMPT_CACHE_AGENTS` | `automation` | Agents whose context is cached (`triage`, `architect`, `automation`, or `all`). |
| `PROMPT_CACHE_TTL` | `5m` | `5m` (write 1.25x) or `1h` (write 2x). |
| `SPEC_REPAIR_ATTEMPTS` | `2` | How often Opus may fix a spec that won't load (0–3). |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama's OpenAI-compatible endpoint. |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Local model used by Agent 4. |
| `OLLAMA_TIMEOUT_SECONDS` | `300` | Timeout for each Ollama request. |
| `TEST_REPO_LOCAL_PATH` | – | **Required.** Absolute path of the Playwright project on *this* machine. |
| `TEST_REPO_NO_GIT` | `true` in `.env.example` | `true` = Mode A: write the spec and run it in place, no git operations. `false` = Mode B: clone/branch/commit. |
| `GITHUB_TOKEN` | – | Mode B only. PAT used to push `qa/*` branches. |
| `TEST_REPO_URL` | – | Mode B only. HTTPS clone URL of the Playwright repo. |
| `TEST_REPO_BASE_BRANCH` | `main` | Branch that QA branches are created from. |
| `TEST_SPEC_DIR` | `tests/qa` | Where spec files are written, relative to the repo root. |
| `GIT_PUSH_ENABLED` | `true` | Set to `false` to commit locally only (useful for dry runs). |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | `QA Agent` / noreply | Commit identity. |
| `GIT_TIMEOUT_SECONDS` | `120` | Timeout for each git command. |
| `PACKAGE_MANAGER` | `npm` in `.env.example` | `npm` or `pnpm`. Must match the lockfile committed in the test project. |
| `SKIP_DEPENDENCY_INSTALL` | `true` in `.env.example` | Skip the install step when `node_modules` is already present and human-managed. |
| `PNPM_INSTALL_TIMEOUT_SECONDS` | `600` | Timeout for the dependency install. |
| `PLAYWRIGHT_TIMEOUT_SECONDS` | `1800` | Timeout for the Playwright test run. |
| `PLAYWRIGHT_INSTALL_BROWSERS` | `false` | Install Chromium before every run. |
| `PLAYWRIGHT_EXTRA_ARGS` | `--reporter=line` | Extra CLI arguments for `playwright test`. |
| `PAPERCLIP_API_URL` | – | Paperclip server, for example `http://localhost:3100` (no `/api`). Leave empty to disable Paperclip. |
| `PAPERCLIP_API_KEY` | – | API key of the QA agent in Paperclip. |
| `PAPERCLIP_PROJECT` | – | Project name or UUID that QA issues are filed under (optional). |
| `PAPERCLIP_ENFORCE_BUDGET` | `true` | Refuse Claude calls while Paperclip has the agent paused or over budget. |
| `XRAY_PROJECT_KEY` | ticket prefix | Jira project the Xray tests are created in. |
| `XRAY_TEST_FOLDER` | `AI QA/{ticket_id}` | Xray test-repository folder. |
| `XRAY_REQUIREMENT_LINK_TYPE` | `Test` | Jira link type used to link each test to the story. |
| `XRAY_INCLUDE_PRIORITY` | `false` | Send priority names (Highest…Lowest). Only enable this if your Jira uses those names. |
| `MAX_CLOUD_TOKENS_PER_RUN` | `250000` | Hard cap on Claude tokens per ticket (`0` = off). Prompt-cache reads don't count. |
| `MAX_CLOUD_TOKENS_PER_DAY` | `2000000` | Hard cap on Claude tokens per day, in memory (`0` = off). |
| `QA_SERVICE_API_KEY` | – | If set, callers must send it in the `X-API-Key` header. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for `python -m taf serve` / `python main.py`. |
| `MAX_DIFF_CHARS`, `MAX_ENV_LOG_CHARS`, `MAX_EXEC_LOG_CHARS_FOR_LLM` | see `.env.example` | Prompt size limits. Longer inputs are truncated in the middle. |
| `CALLBACK_TIMEOUT_SECONDS` | `30` | Timeout for the n8n callback request. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `OLLAMA_MAX_TOKENS` | `4096` | Output cap per local-model call. |
| `OLLAMA_MAX_TEST_CASES` | `5` | Plan-size cap for the Test Architect on a local model. |
| `PAGE_CONTRACT_PATH` | – | Markdown file of real roles/labels/test IDs for the app under test. Relative paths are from the project root. Strongly recommended. |
| `QA_DB_PATH` | `qa_runs.db` | SQLite file holding run history and per-phase artifacts. Created on first start. |
| `DASHBOARD_TARGET_URL` | – | Target URL used by runs started from the dashboard buttons. |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | – | Set all three to make the board poll Jira. With any of them missing it reads `SEED_TICKETS_PATH`. |
| `SEED_TICKETS_PATH` | `data/seed_tickets.json` | Offline queue; ships with TB-601. |
| `SKILLS_ENABLED` / `SKILLS_ON_LOCAL` / `SKILLS_MAX_CHARS` / `SKILLS_DIR` | `true` / `false` / `12000` / `skills` | See [skills/README.md](skills/README.md). |
| `JIRA_JQL` | see `.env.example` | Which tickets the board shows. |
| `JIRA_POLL_SECONDS` / `JIRA_TIMEOUT_SECONDS` | `60` / `20` | Jira poll interval and request timeout. |

## Running

Start the service (development mode, with auto-reload):

```bash
python -m taf serve --reload
```

`uvicorn main:app --host 127.0.0.1 --port 8000 --reload` is equivalent. For a
long-running process drop `--reload` and keep a **single worker**: runs share one
test-repo working tree, and the n8n run registry is held in memory.

Check the configuration:

```bash
python -m taf doctor
```

```bash
curl -s http://localhost:8000/health
```

Interactive API docs are at http://localhost:8000/docs.

### Dashboard

<http://127.0.0.1:8000/dashboard> is a local board that joins the QA queue to live
pipeline state. Each row is a ticket with a pipeline strip (triage → architect →
automation → report → closeout) and buttons that start a single stage; stages are
cumulative, so `architect` runs triage first. "What the agent did" opens the
extracted criteria, the test plan and the generated spec for a finished run.

The queue comes from Jira when `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`
are all set. Otherwise the board reads [data/seed_tickets.json](data/seed_tickets.json)
and shows an amber "Jira is not configured" banner — that is the expected state
for a fresh clone, and the sample ticket in that file is enough to exercise the
whole pipeline.

Run history and per-phase artifacts are persisted by [taf/runs/store.py](taf/runs/store.py)
into the SQLite file at `QA_DB_PATH` (`qa_runs.db` by default, created on first
start and git-ignored), so the board survives a restart.

The dashboard is deliberately **unauthenticated** and the service binds to
`127.0.0.1`. Putting `QA_SERVICE_API_KEY` into a URL or cookie to protect a page
you open in a browser would be worse than not protecting it; the `/qa/*` API keeps
its key. Do not expose this port.

The same thing over HTTP (or use `python -m taf run`, which needs no server):

```bash
curl -s -X POST http://127.0.0.1:8000/dashboard/api/start -H 'Content-Type: application/json' -d '{"ticket_id":"TB-601","stage":"triage","provider":"offline"}'
```

```bash
curl -s http://127.0.0.1:8000/dashboard/api/board
```

```bash
curl -s -X POST http://127.0.0.1:8000/dashboard/api/provider -H 'Content-Type: application/json' -d '{"provider":"ollama"}'
```

`provider` in the start body is optional (default: the service's current provider).

On Windows use `Invoke-RestMethod`, not `curl` — see [docs/DEMO.md](docs/DEMO.md), which has
the PowerShell form of every command here.

## API

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
  "db_logs": "optional"
}
```

Response: `{ "run_id": "…", "ticket_id": "SHOP-1234", "status": "queued", "status_url": "/qa/runs/…" }`.

If a run for the same ticket is already in progress, the service returns `409`.

### `GET /qa/runs/{run_id}`

Returns the run's status (`queued`, `running`, `completed` or `crashed`), the outcome, the branch, token usage, errors and the final report.

### Callback to n8n

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

`status` is either `"QA Passed"` or `"QA Failed"`. `xray_tests_json` is ready to send as-is to Xray Cloud's `POST /api/v2/import/test/bulk`. Use `metadata.run_id` as an idempotency key in n8n: the callback is retried on connection errors, 429 and 502–504.

## n8n workflow outline

1. **Jira Trigger**: fires on an issue transition to *Ready for QA*.
2. **GitHub node**: fetches the linked PR's diff (`Accept: application/vnd.github.diff`).
3. **Azure / DB nodes**: pull recent logs.
4. **HTTP Request**: `POST http://<host>:8000/qa/runs` with the `X-API-Key` header.
5. **Webhook (callback)**: receives the result, then:
   - transitions the Jira issue according to `status`;
   - adds `report_markdown` as a comment;
   - imports `xray_tests_json` through Xray Cloud's bulk import API. Each test is created with its steps, filed in a per-ticket folder, and linked to the story;
   - attaches `xray_csv_content` to the ticket as an audit copy and manual fallback.

   The CSV uses one row per step and repeats the `Test ID` for later steps. Its columns are: `Test ID, Summary, Test Type, Priority, Labels, Requirement, Preconditions, Action, Data, Expected Result`. Labels are separated by `;`.

## Operational notes

- Re-running a ticket imports a **new** set of Xray tests; it doesn't update the previous set.
- Run history and per-phase artifacts are persisted to SQLite (`QA_DB_PATH`), so the dashboard survives a restart. The *active* run registry and the daily budget counter still live **in memory** and reset on restart. Keep a single worker; move these to Redis or Postgres if you need more than one.
- The spec is written as `<TICKET>.spec.ts`. The ticket ID is validated, which prevents path traversal and invalid branch names.
- The report header (status, counts, branch, tokens) is built in Python. The local model only formats the log details, so it cannot change the verdict.

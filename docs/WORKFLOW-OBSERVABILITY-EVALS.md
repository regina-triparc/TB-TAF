# Workflow, observability and evals

What changed in the QA workflow (LangGraph checkpoints, validation gate, approval,
pull requests), how to trace it in Langfuse, and how to measure agent quality with
the eval suite.

- [Migrating an existing install](#migrating-an-existing-install)
- [Environment variables](#environment-variables)
- [Running an ordinary QA workflow](#running-an-ordinary-qa-workflow)
- [Approvals, resuming and pull requests](#approvals-resuming-and-pull-requests)
- [Langfuse: setup and inspecting traces](#langfuse-setup-and-inspecting-traces)
- [Running the eval suite](#running-the-eval-suite)
- [Evaluation results in Langfuse](#evaluation-results-in-langfuse)
- [Limitations](#limitations)

## Migrating an existing install

1. `pip install -r requirements.txt` (adds `langgraph-checkpoint-sqlite` and
   `langfuse`; pins LangGraph / LangChain to their current major versions).
2. Nothing to migrate in `qa_runs.db`: its schema is unchanged. Runs and phases
   gain the state value `waiting`, and the phases `approval` and `publish`.
   Workflow checkpoints go to a **new** file, `qa_checkpoints.db`
   (`QA_CHECKPOINT_DB_PATH`, git-ignored like every `*.db`).
3. Review the new settings below. Two defaults change behaviour:
   - `REQUIRE_APPROVAL=true` - a full run that would push now **waits for your
     approval** instead of pushing on its own.
   - Push happens **after** validation passes, not before the tests run.
   `PR_CREATE_ENABLED` is `false` by default: no pull request is opened until you
   turn it on.
4. `python -m taf doctor` shows the workflow settings and whether Langfuse is on.
5. `python -m unittest` runs the offline tests (no model, network or app needed).

Behaviour changes worth knowing:

| Before | Now |
|---|---|
| Dashboard / CLI called the agents one by one; only n8n used the graph | Everything runs the compiled, checkpointed graph |
| Spec committed and pushed before Playwright ran, even if it never loaded | Pushed only after the static validation passes and a person approves |
| `qa/<TICKET>` reset with `checkout -B`; rejected push retried with `--force-with-lease` | The branch continues from `origin/qa/<TICKET>`; never force-pushed; a rejected push is reported |
| Only `playwright --list` checked the spec | Plus: no Node APIs / dynamic code, imports exist, `tsc --noEmit` and ESLint (when the repo has them) |
| The agent saw a page contract and the diff | It also sees the test repo's page objects, fixtures, helpers and an example spec |
| A crash lost the run | `python -m taf resume <run_id>` continues from the last completed step |
| `--stage automation` committed and pushed | `--stage automation` stops after review: nothing is committed; `full` publishes |

## Environment variables

New in `.env` (see `.env.example` for comments). Secrets only ever come from the
environment or `.env`, which is git-ignored.

| Variable | Default | Meaning |
|---|---|---|
| `QA_CHECKPOINT_DB_PATH` | `qa_checkpoints.db` | LangGraph checkpoints (resume after a crash or an approval). |
| `REQUIRE_APPROVAL` | `true` | Pause before a push / PR until someone approves. Not asked when nothing leaves the machine (push and PR off, or in-place mode). |
| `PR_CREATE_ENABLED` | `false` | Open a pull request after an approved push. |
| `GITHUB_TOKEN` | - | Fine-grained token, test repo only: *Contents: read and write* (push) and *Pull requests: read and write*. Without it, PRs use the GitHub CLI (`gh`) if it is signed in. |
| `SPEC_REPAIR_ATTEMPTS` | `2` | Repairs after the first spec (0-3). Applies to validation failures and review blockers. |
| `REVIEW_WITH_LLM` | `false` | Add a model review of the spec (route `review`: `CLAUDE_REVIEW_MODEL` / `_EFFORT` / `_MAX_TOKENS`, or `OLLAMA_REVIEW_MODEL`). |
| `REPO_CONTEXT_MAX_CHARS` | `6000` | How much of the test repo's conventions the Automation Engineer sees. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | - | Turn tracing on. |
| `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`) | Langfuse Cloud (EU) | Your Langfuse, e.g. `https://us.cloud.langfuse.com` or a self-hosted URL. |
| `LANGFUSE_ENABLED` | `true` | `false` turns tracing off even with keys. |
| `LANGFUSE_ENVIRONMENT` | `local` | Langfuse environment of production runs (evals always use `eval`). |
| `LANGFUSE_CAPTURE` | `truncated` | `metadata` (no prompt/response text), `truncated` (text cut to `LANGFUSE_MAX_FIELD_CHARS`), or `full`. Secrets are redacted in every mode. |
| `LANGFUSE_MAX_FIELD_CHARS` | `4000` | Truncation length per text field. |
| `LANGFUSE_TIMEOUT_SECONDS`, `LANGFUSE_FLUSH_TIMEOUT_SECONDS` | `5`, `10` | Upper bounds, so an unreachable Langfuse never holds up a run. |

## Running an ordinary QA workflow

For a first local check, keep everything on your machine:

```bash
python -m taf doctor
```

```bash
GIT_PUSH_ENABLED=false PR_CREATE_ENABLED=false python -m taf run TB-601 --stage full --source seed
```

(PowerShell: `$env:GIT_PUSH_ENABLED="false"; $env:PR_CREATE_ENABLED="false"; python -m taf run TB-601 --stage full --source seed`.)

What happens, in order: PR diff → triage → test plan → test repo conventions →
spec → static validation (repairs if needed) → Playwright run → review → approval
(skipped here: nothing would leave the machine) → commit on `qa/TB-601` in the
agent's clone → report. The phases appear on the dashboard (`python -m taf serve`).

Partial runs: `--stage triage`, `--stage architect` (no test repo needed) and
`--stage automation` (spec validated and run, nothing committed).

All Ready-for-QA tickets: the dashboard lists the Jira queue (`JIRA_JQL`); start a
**Full run** per ticket, or `python -m taf run <KEY> --stage full --source jira`.

## Approvals, resuming and pull requests

With push or PR creation on and `REQUIRE_APPROVAL=true`, a full run ends in state
**waiting**. The CLI prints what would be published and the review findings:

```bash
python -m taf resume <run_id> --approve --by "your name"
```

```bash
python -m taf resume <run_id> --reject --note "tests check the wrong page"
```

On the dashboard the row shows **Approve** / **Reject**; expand it to see the
spec, validation, Playwright output and review findings first. A waiting run
survives a service restart (its checkpoint is on disk), and blocks a second run of
the same ticket until decided.

A run that crashed (process killed, machine restarted) or was cancelled:
`python -m taf resume <run_id>`, or **Resume** on the dashboard. Steps that
completed are not repeated. `publish` is safe to re-enter: it finds the commit
already made, the branch already on origin and the PR already open.

Pull requests are opened only when the static validation passed and the run was
approved. If the Playwright run failed, the PR is a **draft** and its description
says so - the failure may be a real defect. The PR body lists the files,
validation results, review findings, approver and (when tracing) the trace link.

**Do not turn on `PR_CREATE_ENABLED` while developing or evaluating** unless you
mean to open real pull requests.

## Langfuse: setup and inspecting traces

1. Create a project in Langfuse (Cloud or self-hosted) and an API key pair.
2. In `.env`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL`
   if you are not on Langfuse Cloud EU.
3. `python -m taf doctor` - "Langfuse tracing on ... credentials accepted".

What a run looks like in Langfuse (**Tracing → Traces**):

| Langfuse | Comes from |
|---|---|
| Trace `qa-run` | One QA run. Its id is derived from the run id (`Langfuse.create_trace_id(seed=run_id)`), so a resumed run (`qa-run.resume`) lands in the same trace, and the run id in the dashboard finds it. |
| Session | The Jira ticket id - all runs of one ticket together (**Sessions**). |
| Tags / metadata | `taf`, `provider:<p>`; `run_id`, `ticket_id`, `target_repo`, `base_branch`. |
| Spans | One per workflow step: `gather_context`, `intake_triager`, `test_architect`, `repo_context`, `generate_spec` (one per attempt), `validate_spec`, `run_tests`, `review_spec`, `approval_gate`, `publish`, `report_synthesizer`. Output = sizes and flags (attempt, validation ok, branch, PR URL), never whole specs or logs. Errors are level `ERROR` with the reason. |
| Generations | One per model call, recorded by Langfuse's LangChain callback **inside the LLM router** - model, provider, token usage (incl. cache reads), latency, and the masked prompt/response. The report model's call too. |
| Scores | `static_validation`, `spec_attempts`, `playwright_passed`, `review_findings`, `approval`, `pull_request_opened` (comment = PR URL). |

Useful views: filter by **Session** = ticket id; by tag `provider:ollama` vs
`provider:claude` to compare cost and latency; **Scores** → `static_validation`
over time; open a failed trace and read the `validate_spec` span's output for the
check that failed. The QA report and the dashboard's report panel link to the
trace.

Privacy: everything sent passes the mask in `taf/observability/tracing.py` -
configured secrets, GitHub/Anthropic tokens, `Authorization` headers and
`password=`-style values are redacted, long text is truncated
(`LANGFUSE_CAPTURE`). Repositories are never uploaded: the diff and the repo
conventions appear only as part of a (truncated) prompt, if at all.

Failure behaviour: no keys, no package, or Langfuse unreachable → one warning,
the run continues untraced; flushing and the trace link are bounded by
`LANGFUSE_FLUSH_TIMEOUT_SECONDS`.

## Running the eval suite

Evals are separate from production runs: no run history, no approval, no commit,
push or PR, and Langfuse only with `--langfuse` (environment `eval`).

```bash
python -m taf eval --provider offline
```

The offline provider is the free plumbing check (every hard check passes, several
soft ones do not, by design). Real measurements:

```bash
python -m taf eval --provider ollama --label baseline
```

```bash
python -m taf eval --provider ollama --with-repo --judge --label prompt-v2
```

```bash
python -m taf eval compare evals/results/<baseline>.json evals/results/<prompt-v2>.json
```

- `--with-repo` adds `tsc`, ESLint and `playwright --list` in the test repo (spec
  written temporarily on the base branch, removed afterwards); `--execute` also
  runs the tests against the case's `target_url` - a pass is reported, never
  treated as proof of coverage.
- `--judge [scenario_quality.v1]` adds an LLM judge for what rules cannot check
  (criteria fidelity, scenario relevance and correctness, whether tests assert the
  expected results, handling of vague tickets). `--judge-provider claude` grades a
  local run with Claude.
- `--case <id>` (repeatable), `--stage triage|architect|automation`.

See [evals/README.md](../evals/README.md) for the dataset, the dimensions and how
to add a case or a judge version.

## Evaluation results in Langfuse

With `--langfuse`, each case is a trace in environment **eval**, tagged `eval` and
`case:<id>`, with scores `eval.passed`, `eval.<stage>.hard`, `eval.<stage>.soft`
and `judge.<dimension>` (comment: judge prompt version and model). Filter the
traces by environment `eval` and compare scores across `--label`s, models or
dates in **Scores** / dashboards. The JSON in `evals/results/` remains the
reference: it also records prompt hashes, dataset version and git commit.

## Limitations

- Type checking and linting run only when the test repo installs `typescript` /
  `eslint` itself. tripbuilder-e2e currently has neither, so the gate there is the
  safety and import checks plus `playwright --list`.
- One shared clone of the test repo: steps take turns under a lock, so parallel
  runs of different tickets queue at the repo steps.
- An approval waits indefinitely; there is no timeout or reminder.
- The n8n path (`POST /qa/runs`) also stops at the approval gate; its in-memory
  status says `waiting_approval`, and after `taf resume` the closeout calls n8n.
- Checkpoints keep each run's full state (including the condensed diff and logs);
  delete `qa_checkpoints.db` to reclaim space - finished runs do not need it.

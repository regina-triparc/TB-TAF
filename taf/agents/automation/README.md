# Agent 3 - TS Automation Engineer

Writes the Playwright spec for the plan's automatable cases - text only. Writing,
validating, running and publishing it are deterministic steps in `taf/steps/`; the
graph sends a rejected spec back here with the reason (`spec_feedback`).

| | |
|---|---|
| Runs on | the `automation` route: Claude (`CLAUDE_AUTOMATION_*`, Opus by default), Ollama, or offline |
| Reads | `test_cases`, `triage_analysis`, `github_diff` (half the diff budget), the page contract (`PAGE_CONTRACT_PATH`) |
| Reads also | `repo_context` (the test repo's page objects - with the locator map of those marked "Verified against" - fixtures, helpers, example spec), `live_snapshot` (the app's accessibility tree, captured for this run), `spec_feedback` on a repair |
| Writes | `generated_typescript_code`, `spec_attempts`, `spec_feedback` |
| Skills | `agents: automation`: the four Playwright skills |
| Stage | `automation`, `full` |

## Where it sits in the workflow (tripbuilder-e2e)

1. `repo_context` (deterministic) prepares the test repo and reads its conventions;
   they are part of this agent's prompt, with exact import paths.
   `ui_snapshot` (deterministic, read-only) then opens the live app with the test
   repo's saved session and records the accessibility tree of each state listed in
   `<page contract>.snapshot.json` (forms, dropdown options, sheets, pickers).
2. **This agent** writes `e2e/qa/<TICKET>.spec.ts` as text.
3. `validate_spec` rejects unsafe or unloadable specs (Node APIs, missing imports,
   `playwright test --list`, `tsc`, ESLint). A fixable failure comes back here with
   the checker output (`repair-prompt.md`), up to `SPEC_REPAIR_ATTEMPTS` times.
4. `run_tests` runs it with `PLAYWRIGHT_EXTRA_ARGS` (`--project=qa-agent`), plus the
   JSON reporter for per-test results. `repair_from_run` sends the failing tests back
   here with their error and page snapshot (`run-repair-prompt.md`), up to
   `RUN_REPAIR_ATTEMPTS` times. A repair that drops an `expect()` without quarantining
   the test (`test.fixme`) or touches a passing test into fixme is rejected before it
   runs; a kept one must fix or quarantine a failing test and break none. Only the
   failing tests (and changed passing ones) are rerun.
   `review_spec` then flags helper functions in the spec, unnamed `page.getByRole(...)`
   and 12-hour option values, and
   `review_spec` can also send it back once for blockers (fixed sleeps, `.only`,
   hard-coded secrets).
5. After approval, `publish` commits the spec with `xray/<TICKET>/` on `qa/<TICKET>`.

The offline provider writes nothing to the repo: its placeholders must never
replace real tests.

## Files

| File | What it is |
|---|---|
| `prompt.md` | The system prompt: the hard rules for a generated spec. |
| `repair-prompt.md` | The follow-up turn sent when a check rejects the spec. `{spec}` and `{error}` are filled in. |
| `run-repair-prompt.md` | The follow-up turn sent when tests fail at run time: the failures with their page snapshots, and the rule that expectations are never loosened. |
| `agent.py` | Prompt building (ticket, plan, diff, repo conventions, page contract) and the repair turn. |

The checks and repo steps are in `taf/steps/`; the git and Playwright commands in
`taf/integrations/github/repository.py`.

## Change its behaviour

- **What it is told:** edit `prompt.md`; for team practices, add a skill
  (`skills/<name>/SKILL.md` with `agents: automation`).
- **Real selectors:** keep the page contract current. It has the biggest single
  effect on whether specs pass.
- **Check the effect:** `python -m taf eval --stage automation` grades the spec
  without touching any repo; add `--with-repo` for tsc / ESLint / `--list`.

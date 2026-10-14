# Agent 3 - TS Automation Engineer

Writes the Playwright spec for the plan's automatable cases and gets it to load.
It then commits the spec together with the ticket's Xray test cases, and runs it.

| | |
|---|---|
| Runs on | the `automation` route: Claude (`CLAUDE_AUTOMATION_*`, Opus by default), Ollama, or offline |
| Reads | `test_cases`, `triage_analysis`, `github_diff` (half the diff budget), the page contract (`PAGE_CONTRACT_PATH`) |
| Writes | `generated_typescript_code`, `spec_file_path`, `xray_files`, `git_branch`, `git_pushed`, `execution_logs`, `execution_passed` |
| Skills | `agents: automation`: the four Playwright skills |
| Stage | `automation`, `full` |

## What it does in the test repo (tripbuilder-e2e)

1. Prepares the repo: its own clone on branch `qa/<TICKET>`, or your working copy
   in place (`TEST_REPO_NO_GIT=true`).
2. Writes `xray/<TICKET>/`: `<TICKET>.xray.json`, `<TICKET>.xray.csv` and
   `<TICKET>.test-plan.md`.
3. Writes `e2e/qa/<TICKET>.spec.ts`, and runs `playwright test --list`. If the spec
   does not load, the loader error goes back to the model (`repair-prompt.md`), up
   to `SPEC_REPAIR_ATTEMPTS` times.
4. Commits the spec and the Xray files in one commit, and pushes the branch when
   `GIT_PUSH_ENABLED=true`. In place, nothing is committed.
5. Runs the spec with `PLAYWRIGHT_EXTRA_ARGS` (`--project=qa-agent`).

The offline provider writes nothing to the repo: its placeholders must never
replace real tests.

## Files

| File | What it is |
|---|---|
| `prompt.md` | The system prompt: the hard rules for a generated spec. |
| `repair-prompt.md` | The follow-up turn sent when Playwright cannot load the spec. `{spec}` and `{error}` are filled in. |
| `agent.py` | Context building, the repair loop, and the repo steps above. |

The git and Playwright commands themselves are in
`taf/integrations/github/repository.py`.

## Change its behaviour

- **What it is told:** edit `prompt.md`; for team practices, add a skill
  (`skills/<name>/SKILL.md` with `agents: automation`).
- **Real selectors:** keep the page contract current. It has the biggest single
  effect on whether specs pass.
- **Check the effect:** `python -m taf eval --stage automation`, which grades the
  spec without touching any repo.

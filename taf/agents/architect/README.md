# Agent 2 - Test Architect

Designs a risk-based test plan from the triage analysis, and turns it into Xray test
cases.

| | |
|---|---|
| Runs on | the `architect` route: Claude (`CLAUDE_ARCHITECT_*`), Ollama, or offline |
| Reads | `triage_analysis`, `description`, `github_diff` (a third of the diff budget) |
| Writes | `test_plan` (Markdown), `test_cases`, `xray_tests_json` (Xray Cloud import), `xray_csv_content` (Xray CSV) |
| Skills | `agents: architect`, e.g. `risk-based-test-design` |
| Stage | `architect`, `automation`, `full` |

The Xray output is not sent anywhere from here. The Automation Engineer writes it
to the test repo under `xray/<TICKET>/`, next to the spec, for review in a pull
request. `python -m taf xray export` then imports the reviewed version into Jira.

## Files

| File | What it is |
|---|---|
| `prompt.md` | The system prompt. |
| `local-model-limits.md` | Appended on Ollama routes only: caps the plan size, because a small model cannot finish a 20-case plan. `{max_cases}` is replaced with `OLLAMA_MAX_TEST_CASES`. |
| `agent.py` | Builds the context, calls the router, and renders the plan as Markdown and Xray. |

The Xray formats themselves are built in `taf/integrations/jira/xray.py`.

## Change its behaviour

- **What it is told:** edit `prompt.md`, or `local-model-limits.md` for local runs.
- **Which model:** set `CLAUDE_ARCHITECT_*` or `OLLAMA_ARCHITECT_MODEL`.
- **Check the effect:** `python -m taf eval --stage architect`.

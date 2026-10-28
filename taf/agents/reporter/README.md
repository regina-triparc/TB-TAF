# Agent 4 - Report Synthesizer

Turns the raw git, npm and Playwright logs into a short Markdown QA report.

| | |
|---|---|
| Runs on | always the local model (`OLLAMA_MODEL`) - formatting logs is cheap work. Offline runs use the built-in fallback. |
| Reads | `execution_logs`, `execution_passed`, `errors`, `triage_analysis`, run costs |
| Writes | `final_report`, `execution_passed`, `report_html_path` |
| Stage | `full` (and every n8n run) |

The verdict header (status, counts, branch, cost, models) is built in Python. The
model only formats the log details, so it cannot change a result. If Ollama is
down or times out, a deterministic report is used and the run still completes.

## HTML report

Every run also gets `<REPORTS_DIR>/<TICKET>-<run>.html` (default `reports/`), linked
from the dashboard's Report section as **Open HTML report** and printed by `taf run`.
It is one self-contained file - styles, data and failure screenshots inline - so it
can be opened from disk or attached to Jira. No model writes into it.

| Section | Built from |
|---|---|
| Header and verdict | ticket, target, run ID, spec, branch, PR, trace, Playwright counts |
| Automation results | `playwright.tests`: status, duration, error and screenshots per test |
| Gaps and open points | criteria no case covers, automatable cases that did not run, manual-only cases, failed validation checks, review findings, triage questions and risks, pipeline errors |
| Before you start | environment, build and tester fields; the rerun command for the whole spec |
| One card per test case | preconditions, steps, each expected result as an assertion to mark Pass / Fail / Blocked / Unverified, screenshots, the rerun command for that case, the case as JSON |
| Raw data | acceptance criteria, the Xray import payload, automation results as JSON |
| Results to paste | a Markdown table for Jira that follows the marks |

Marks start from the automation run: a passed automated test marks its assertions
Pass; a failed one leaves them for the tester, with the error shown. A QA re-runs the
checklist by hand in the same file: marks and notes are kept in that browser, and
**Reset to automation** / **Clear marks** start over.

## Files

| File | What it is |
|---|---|
| `prompt.md` | The system prompt: the report's sections and rules. |
| `agent.py` | Builds the verdict header, calls the model, falls back when it is unavailable. |
| `html_report.py` | Builds the HTML report's data from the run state and writes the file. |
| `report-template.html` | The HTML report's page; `html_report.py` fills in the data. |

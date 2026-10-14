# Agent 4 - Report Synthesizer

Turns the raw git, npm and Playwright logs into a short Markdown QA report.

| | |
|---|---|
| Runs on | always the local model (`OLLAMA_MODEL`) - formatting logs is cheap work. Offline runs use the built-in fallback. |
| Reads | `execution_logs`, `execution_passed`, `errors`, `triage_analysis`, run costs |
| Writes | `final_report`, `execution_passed` |
| Stage | `full` (and every n8n run) |

The verdict header (status, counts, branch, cost, models) is built in Python. The
model only formats the log details, so it cannot change a result. If Ollama is
down or times out, a deterministic report is used and the run still completes.

## Files

| File | What it is |
|---|---|
| `prompt.md` | The system prompt: the report's sections and rules. |
| `agent.py` | Builds the verdict header, calls the model, falls back when it is unavailable. |

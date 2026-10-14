# Agent 1 - Intake Triager

Turns a ticket into something testable: explicit acceptance criteria, the areas
the change touches, concrete risks, and open questions for the team.

| | |
|---|---|
| Runs on | the `triage` route of the LLM router: Claude (`CLAUDE_TRIAGE_*`), Ollama, or offline |
| Reads | `summary`, `description`, `target_url`, `github_diff` (condensed for its budget), `environment_logs` |
| Writes | `triage_analysis`: a `TriageAnalysis` (see `../schemas.py`) |
| Skills | `skills/*/SKILL.md` with `agents: triage`, on Claude routes |
| Stage | first in every stage: `triage`, `architect`, `automation`, `full` |

## Files

| File | What it is |
|---|---|
| `prompt.md` | The system prompt, sent as written. Edit it here, no Python changes. |
| `agent.py` | Builds the ticket context (`build_prompt`) and calls the router for structured output. |

## Change its behaviour

- **What it is told:** edit `prompt.md`.
- **Which model:** set `CLAUDE_TRIAGE_MODEL` / `_EFFORT`, `OLLAMA_TRIAGE_MODEL`, or
  `CLOUD_AGENTS=triage` to keep it on Claude in a local run.
- **How much diff it sees:** `MAX_DIFF_CHARS`, or `OLLAMA_MAX_DIFF_CHARS` on Ollama.
- **Check the effect:** `python -m taf eval --stage triage`.

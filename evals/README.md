# Evals

Evals answer "did that change make the agents better or worse?" - a new model, a
prompt edit, a new skill, a switch from Claude to Ollama. Each case is a ticket
with expectations; the runner pushes it through the agents via the LLM router and
grades the output deterministically.

```bash
python -m taf eval                                   # every case, default provider
python -m taf eval --provider ollama                 # claude | ollama | auto | offline
python -m taf eval --case tb-601 --stage architect   # one case, stop after the Test Architect
python -m taf eval --provider offline                # plumbing check, no model, runs anywhere
```

Nothing touches the test repository and Playwright is not run - the Automation
Engineer's spec is generated and graded as text. Results are printed and saved to
`evals/results/<timestamp>-<provider>.json` (git-ignored) for comparison.

## Layout

```
evals/
  cases/*.json     one ticket + expectations per file
  graders.py       deterministic checks for triage, plan and spec
  runner.py        runs the agents and grades them
  results/         output, git-ignored
```

## Hard and soft checks

- **Hard** (`x` in the output): requirements. Any failure fails the case and the
  command exits 1, so this can gate CI.
- **Soft** (`~`): quality signals - uses `getByRole`, uses `test.step`, few
  `test.fixme` placeholders, no "works correctly" expected results. Reported,
  never failing.

The `offline` provider passes every hard check and misses several soft ones by
design: it proves the pipeline runs, not that the output is good.

## Adding a case

```json
{
  "id": "my-ticket",
  "ticket": {
    "key": "ABC-1",
    "summary": "...",
    "description": "... ACCEPTANCE CRITERIA\n1. ...\n2. ...",
    "target_url": "https://app.example.test"
  },
  "expect": {
    "triage":     {"min_criteria": 3, "risk_level_in": ["medium", "high"], "must_mention": ["..."]},
    "architect":  {"min_cases": 3, "max_cases": 15, "min_automatable": 2, "must_cover": ["..."]},
    "automation": {"min_tests": 2, "require": ["test.describe"], "forbid": ["waitForTimeout"], "prefer": ["getByRole"]}
  }
}
```

Use `"from_seed": true` instead of `summary`/`description` to read the ticket
from `data/seed_tickets.json`. Keep `must_mention` / `must_cover` to facts any
correct answer has to contain, in the ticket's own words.

Model-backed evals cost money on Claude (each case is three agent calls) and take
minutes on Ollama - run them when you change a prompt, skill or model, not on
every commit. The offline provider is the per-commit check.

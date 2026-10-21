# Evals

Evals answer "did that change make the agents better or worse?" - a new model, a
prompt edit, a new skill, a switch from Claude to Ollama. Each case is a ticket
with expectations; the runner pushes it through the agents via the LLM router and
grades the output deterministically, optionally adding test-repo checks and an
LLM judge.

```bash
python -m taf eval                                   # every case, default provider
python -m taf eval --provider ollama                 # claude | ollama | auto | offline
python -m taf eval --case tb-601 --stage architect   # one case, stop after the Test Architect
python -m taf eval --provider offline                # plumbing check, no model, runs anywhere
python -m taf eval --with-repo                       # + playwright --list, tsc, ESLint in the test repo
python -m taf eval --with-repo --execute             # + run the generated tests (needs the target app)
python -m taf eval --judge                           # + LLM judge, judges/scenario_quality.v1.md
python -m taf eval --judge --judge-provider claude   # grade a local model's output with Claude
python -m taf eval --langfuse --label prompt-v2      # + traces and scores in Langfuse (environment "eval")
python -m taf eval compare A.json B.json             # what changed between two result files
```

Evals are kept apart from production QA runs: they create no run history, need
no approval, and never commit, push or open a PR. With `--with-repo` the spec is
written onto the test repo's base branch only while it is checked, then removed.
Langfuse is off unless `--langfuse`, and then uses the `eval` environment.

Results are printed and saved to `evals/results/<timestamp>-<provider>.json`
(git-ignored, or `--out`). Each file records what is needed to compare runs:
`label`, provider and per-agent models, `dataset_version` (hash of `cases/`),
a hash of every prompt and skill, the git commit (and whether the tree was dirty),
the options used and the judge prompt version and model.

## Layout

```
evals/
  cases/*.json            one ticket + expectations per file (the dataset, version-controlled)
  graders.py              deterministic checks, each tagged with a dimension
  judge.py                LLM-as-a-judge, versioned rubric in judges/
  judges/<name>.vN.md     judge prompts - never edited in place; a new rubric is a new version
  runner.py               runs the agents, grades, saves, compares
  results/                output, git-ignored
```

## Dimensions

| Dimension | Checked by | Examples |
|---|---|---|
| requirements | rules | enough criteria, key facts present, risk level |
| traceability | rules | every case cites a criterion, every criterion has a case, every automatable case has a `[TC-xxx]` test |
| scenarios | rules + judge | plan size, steps, no "works correctly"; judge: relevance and correctness |
| duplicates | rules | near-duplicate criteria, cases or tests |
| ambiguity | rules + judge | vague tickets get open questions; judge: flagged, not guessed |
| selectors | rules | `getBy*` over raw CSS / XPath |
| conventions | rules | required / forbidden constructs, page objects and fixtures reused |
| safety_scope | rules | no Node APIs or dynamic code; nothing on out-of-scope areas |
| typescript | test repo (`--with-repo`) | `playwright --list`, `tsc --noEmit`, ESLint |
| execution | test repo (`--execute`) | the generated tests pass - reported, never proof of coverage |

The judge (`--judge`) only scores what rules cannot: criteria fidelity, scenario
relevance, scenario correctness, whether tests assert the expected results, and
ambiguity handling (1-5 each, with a rationale). Its scores are reported next to
the checks; they do not change pass/fail.

## Hard and soft checks

- **Hard** (`x` in the output): requirements. Any failure fails the case and the
  command exits 1, so this can gate CI.
- **Soft** (`~`): quality signals - selectors, duplicates, coverage of every
  criterion, placeholders. Reported, never failing.

The `offline` provider passes every hard check and misses several soft ones by
design: it proves the pipeline runs, not that the output is good.

## The dataset

| Case | What it probes |
|---|---|
| `tb-601` | the real TripBuilder story (from `data/seed_tickets.json`) |
| `sample-password-reset` | a generic, app-independent UI + API ticket |
| `ambiguous-export` | no acceptance criteria: open questions instead of invented behaviour |
| `api-rate-limit` | API-only change: `request` fixture, status codes, headers; out-of-scope areas |
| `duplicate-prone-filters` | criteria that repeat each other: no duplicated cases |
| `scoped-copy-change` | a tiny change: no tests for excluded checkout / payments |

`tb-601` reads its ticket through the ticket source: run with `TICKET_SOURCE=seed`
(as the tests do) to grade the seeded text rather than the live Jira issue.

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
    "triage":     {"min_criteria": 3, "risk_level_in": ["medium", "high"], "must_mention": ["..."],
                   "ambiguous": false, "min_questions": 1, "should_ask_about": ["..."]},
    "architect":  {"min_cases": 3, "max_cases": 15, "min_automatable": 2, "must_cover": ["..."],
                   "require_traceability": true},
    "automation": {"min_tests": 2, "require": ["test.describe"], "forbid": ["waitForTimeout"],
                   "prefer": ["getByRole"], "out_of_scope": ["billing"], "reuse": ["../pages/landing.page"]}
  }
}
```

Use `"from_seed": true` instead of `summary`/`description` to read the ticket
from `data/seed_tickets.json`. Keep `must_mention` / `must_cover` to facts any
correct answer has to contain, in the ticket's own words. Changing a case changes
`dataset_version`, and `compare` then warns that the datasets differ.

## Comparing

```bash
python -m taf eval --provider ollama --label baseline --out evals/results/baseline.json
# change a prompt, a model, a skill...
python -m taf eval --provider ollama --label candidate --out evals/results/candidate.json
python -m taf eval compare evals/results/baseline.json evals/results/candidate.json
```

`compare` lists what differs in the setup (models, changed prompt files, code
commit, dataset), then hard/soft per case and stage (`FIXED` / `BROKE`), judge
means, and checks passed per dimension.

Model-backed evals cost money on Claude (each case is three agent calls, plus one
per judge) and take minutes on Ollama - run them when you change a prompt, skill
or model, not on every commit. The offline provider is the per-commit check.

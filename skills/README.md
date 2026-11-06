# Skills

A skill is team know-how, written in Markdown, that the framework appends to an
agent's system prompt. To teach an agent something, add a folder here; no Python
changes.

```
skills/
  <skill-name>/SKILL.md      one skill (loaded automatically)
  page-contracts/            app-specific selector maps, loaded via PAGE_CONTRACT_PATH (not skills)
```

The test repo's own Claude Code skills, `<TEST_REPO_LOCAL_PATH>/.claude/skills/*/SKILL.md`,
load too (turn off with `SKILLS_FROM_TEST_REPO=false`). Keep repo-specific rules there, next
to the code they describe, so Claude Code in that repo and TAF follow the same rule. Without
an `agents:` line they go to the automation agent, at priority 50, so they load before TAF's
generic skills. A test-repo skill with the same `name` as one here is skipped.

## Format

```markdown
---
name: playwright-locators
description: One line - what this teaches. Shown by `python -m taf skills`.
agents: automation            # comma list: triage, architect, automation
---
The body. Write it as instructions to the agent: short, concrete, with code.
```

### Optional: load a skill only when the run needs it

New skills can say when they apply. A skill without these fields always applies to
its agents (all the original skills), so nothing changes for them.

```markdown
---
name: api-contract-testing
description: Schemas from the documented contract, a test per status code.
agents: automation
layers: api                 # api, ui - the run's layer must be one of them
actions: create, update     # create, update, regression - the run's action must match
triggers: src/**/Controllers/*.cs, negative   # diff path globs or test-case categories
priority: 10                # higher loads first when SKILLS_MAX_CHARS is tight (default 0)
---
```

Long material goes in `<skill>/references/*.md`, kept out of the prompt unless its own
`triggers:` front matter matches the diff or the plan (a reference without triggers is
never loaded automatically). References are never loaded on Ollama routes.

Until tickets are classified, every run is layer `ui`, action `create`.

Nothing is dropped silently: each agent's loaded and skipped skills, with the reason,
are recorded in the run (`skills_used`) and shown on the dashboard, in the report and
on the agent's Langfuse span.

## What is loaded, and when

| Setting | Default | Effect |
|---|---|---|
| `SKILLS_ENABLED` | `true` | Master switch. |
| `SKILLS_ON_LOCAL` | `false` | Also apply skills on Ollama routes. Off by default: a 4–8k context fills fast. |
| `SKILLS_MAX_CHARS` | `12000` | Total characters appended per agent. Skills load by priority, then folder name; one that does not fit is skipped and recorded. |
| `SKILLS_DIR` | `skills` | Folder to load from, relative to the project root. |

Skills never apply to the `offline` provider.

```bash
python -m taf skills        # list skills and which agents use them
```

## Included

| Skill | Agent | Teaches |
|---|---|---|
| `acceptance-criteria-extraction` | triage | Turning ticket prose into atomic, testable criteria |
| `risk-based-test-design` | architect | Choosing and ordering cases by risk; what "automatable" means |
| `playwright-locators` | automation | Locator priority, scoping, strictness |
| `playwright-assertions-and-waiting` | automation | Web-first assertions, no sleeps, slow-loading pages |
| `playwright-spec-structure` | automation | describe / step / independence / auth via storageState |
| `playwright-network-assertions` | automation | Asserting API calls and status codes from the UI |

## Writing a good skill

- One topic per skill. If it needs a table of contents, split it.
- Prefer a short rule plus a code example over paragraphs of advice.
- Say what to do, not only what to avoid.
- Keep app-specific facts (selectors, routes, test data) in `page-contracts/`,
  not in a skill: skills should apply to any app.
- After adding one, check its effect with the evals: `python -m taf eval`.

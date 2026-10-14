# Skills

A skill is team know-how, written in Markdown, that the framework appends to an
agent's system prompt. To teach an agent something, add a folder here; no Python
changes.

```
skills/
  <skill-name>/SKILL.md      one skill (loaded automatically)
  page-contracts/            app-specific selector maps, loaded via PAGE_CONTRACT_PATH (not skills)
```

## Format

```markdown
---
name: playwright-locators
description: One line - what this teaches. Shown by `python -m taf skills`.
agents: automation            # comma list: triage, architect, automation
---
The body. Write it as instructions to the agent: short, concrete, with code.
```

## What is loaded, and when

| Setting | Default | Effect |
|---|---|---|
| `SKILLS_ENABLED` | `true` | Master switch. |
| `SKILLS_ON_LOCAL` | `false` | Also apply skills on Ollama routes. Off by default: a 4–8k context fills fast. |
| `SKILLS_MAX_CHARS` | `12000` | Total characters appended per agent. Skills load in folder-name order until the cap. |
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

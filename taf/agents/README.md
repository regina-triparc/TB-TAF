# Agents

One folder per agent. Each holds `agent.py` (the code), its prompts as Markdown,
and a `README.md` saying what it reads, writes and runs on.

```
taf/agents/
  triage/        Agent 1  Intake Triager        criteria, impacted areas, risks       prompt.md
  architect/     Agent 2  Test Architect        test plan + Xray test cases           prompt.md, local-model-limits.md
  automation/    Agent 3  TS Automation Eng.    spec + xray/ in the test repo, run    prompt.md, repair-prompt.md
  reporter/      Agent 4  Report Synthesizer    logs -> Markdown report               prompt.md
  closeout/      Agent 5  Closeout              optional n8n callback (unused locally)

  base.py        the envelope every agent runs in: events, skipping after an abort, error capture
  graph.py       the LangGraph wiring, and NODES (stage name -> agent) for the dashboard and CLI
  schemas.py     structured outputs: TriageAnalysis, TestPlan, TestCase
  state.py       QAState, the state shared by one run
  prompts.py     load_prompt(): reads an agent's .md prompt
```

The pipeline runs `triage -> architect -> automation -> reporter`, then `closeout`
for n8n runs only. If an agent fails, the run jumps straight to the report.

No agent creates a model client itself. They all go through `taf/llm/router.py`,
which picks Claude, Ollama or offline per agent and per run, and applies the
budget guard.

## Adding or changing an agent

- **Change what an agent is told:** edit its `.md` prompt.
- **Add team know-how for one or more agents:** add a skill under `skills/`.
- **Add an agent:**
  1. Copy a folder.
  2. Decorate its function with `@agent_node(...)` from `base.py`.
  3. Add it to `NODES` and the graph in `graph.py`.
  4. Add it to `STAGE_NODES` in `taf/runs/stages.py` if the dashboard should run it.

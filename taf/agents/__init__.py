"""
The pipeline agents, one module each:

    triage.py      Agent 1 - Intake Triager       acceptance criteria, impacted areas, risk
    architect.py   Agent 2 - Test Architect       test plan + Xray CSV / JSON
    automation.py  Agent 3 - TS Automation Eng.   Playwright spec, repair loop, run
    reporter.py    Agent 4 - Report Synthesizer   logs -> Markdown report
    closeout.py    Agent 5 - Closeout             n8n callback

graph.py wires them into the LangGraph; base.py is the envelope each one runs in.
Agents never build model clients: they go through taf.llm.router.ROUTER.
"""

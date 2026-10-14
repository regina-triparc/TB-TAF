"""
The pipeline agents, one folder each (agent.py + Markdown prompts + README.md):

    triage/      Agent 1 - Intake Triager       criteria, impacted areas, risk
    architect/   Agent 2 - Test Architect       test plan + Xray test cases
    automation/  Agent 3 - TS Automation Eng.   spec + xray/ written to the test repo, run
    reporter/    Agent 4 - Report Synthesizer   logs -> Markdown report
    closeout/    Agent 5 - Closeout             optional n8n callback (not used by local runs)

graph.py wires them into the LangGraph; base.py is the envelope each one runs in.
Agents never build model clients: they go through taf.llm.router.ROUTER.
See README.md in this folder.
"""

"""
TAF - autonomous QA multi-agent framework.

Package map (each folder has one job):

    taf/config.py          settings from .env, logging with secret redaction
    taf/core/              small text / HTTP helpers with no project knowledge
    taf/llm/               the model router (Claude <-> Ollama <-> offline), pricing, budget
    taf/agents/            the five pipeline agents and the LangGraph that wires them
    taf/integrations/      Jira (queue + Xray), GitHub (the Playwright test repo), Paperclip
    taf/skills.py          loads ../skills/*/SKILL.md into agent prompts
    taf/runs/              run history (SQLite), the in-memory registry, the stage runner
    taf/api/               FastAPI app: /health, /qa/runs, /dashboard
    taf/cli.py             `python -m taf ...` - everything above, from a terminal
"""

__version__ = "2.0.0"

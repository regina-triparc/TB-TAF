"""
Smoke tests: `python -m unittest` from the project root.

They run with the offline provider and no external service - no API key, no
Ollama, no Jira, no Playwright project - so they pass on any machine that has run
`pip install -r requirements.txt`. The environment is pinned here, before anything
imports `taf`, so a developer's own .env (Jira token, test repo path) is not used.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="taf-tests-")

os.environ.update({
    "LLM_PROVIDER": "offline",
    "ANTHROPIC_API_KEY": "",
    "CLOUD_AGENTS": "",
    "JIRA_BASE_URL": "",
    "JIRA_EMAIL": "",
    "JIRA_API_TOKEN": "",
    "PAPERCLIP_API_URL": "",
    "PAPERCLIP_API_KEY": "",
    "QA_SERVICE_API_KEY": "",
    "TEST_REPO_LOCAL_PATH": "",
    "SEED_TICKETS_PATH": "data/seed_tickets.json",
    "TICKET_SOURCE": "seed",
    "SOURCE_REPO_URL": "",          # no git/network: PR diffs are skipped with a note
    "TEST_REPO_ENV_FILE": "",
    "QA_DB_PATH": os.path.join(_TMP, "qa_runs.db"),
    "SKILLS_ENABLED": "true",
    "SKILLS_ON_LOCAL": "false",
    "LOG_LEVEL": "WARNING",
})

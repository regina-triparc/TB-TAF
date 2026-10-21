"""
Configuration and logging.

Every setting is read once from the environment (and `.env` at the project root)
into the frozen `SETTINGS` object. Relative paths in settings are resolved against
the project root, not the shell's working directory, so the service and the CLI
behave the same wherever they are started from.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Explicit path: the .env next to the code, whatever the current directory is.
# Variables already present in the environment win (load_dotenv never overrides).
load_dotenv(PROJECT_ROOT / ".env")

LLM_PROVIDERS = ("claude", "ollama", "auto", "offline")


def env_str(name: str, default: str = "") -> str:
    """Read a string env var, stripping whitespace; empty values fall back to default."""
    value = os.getenv(name, "").strip()
    return value or default


def env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to `default` on missing/invalid values."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("qa.config").warning("Invalid integer for %s=%r, using %s", name, raw, default)
        return default


def env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var ("1", "true", "yes", "on" are truthy)."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def project_path(value: str) -> Path:
    """Expand `~` and anchor a relative path at the project root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    """All runtime configuration, resolved once at import time from the environment / .env."""

    # --- LLM router (see taf/llm/router.py) ---------------------------------
    # claude  = agents 1-3 on Claude, the report on Ollama (the original hybrid).
    # ollama  = every agent on Ollama: no Anthropic key, no cloud cost.
    # auto    = claude when ANTHROPIC_API_KEY is set, otherwise ollama.
    # offline = deterministic built-in stand-in, no model at all. For smoke runs,
    #           CI and evals of the pipeline mechanics - never for real QA.
    llm_provider: str = field(default_factory=lambda: env_str("LLM_PROVIDER", "claude").lower())
    # Agents that use Claude even when the provider is local (triage,architect,automation or all).
    cloud_agents: str = field(default_factory=lambda: env_str("CLOUD_AGENTS", ""))

    # --- Cloud LLM (Claude) -------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: env_str("ANTHROPIC_API_KEY"))
    claude_triage_model: str = field(default_factory=lambda: env_str("CLAUDE_TRIAGE_MODEL", "claude-sonnet-5-5"))
    claude_triage_effort: str = field(default_factory=lambda: env_str("CLAUDE_TRIAGE_EFFORT", "medium"))
    claude_triage_max_tokens: int = field(default_factory=lambda: env_int("CLAUDE_TRIAGE_MAX_TOKENS", 16_000))
    claude_architect_model: str = field(default_factory=lambda: env_str("CLAUDE_ARCHITECT_MODEL", "claude-sonnet-5-5"))
    claude_architect_effort: str = field(default_factory=lambda: env_str("CLAUDE_ARCHITECT_EFFORT", "high"))
    claude_architect_max_tokens: int = field(default_factory=lambda: env_int("CLAUDE_ARCHITECT_MAX_TOKENS", 32_000))
    claude_automation_model: str = field(default_factory=lambda: env_str("CLAUDE_AUTOMATION_MODEL", "claude-opus-5-5"))
    claude_automation_effort: str = field(default_factory=lambda: env_str("CLAUDE_AUTOMATION_EFFORT", "high"))
    claude_automation_max_tokens: int = field(default_factory=lambda: env_int("CLAUDE_AUTOMATION_MAX_TOKENS", 64_000))
    claude_timeout_s: int = field(default_factory=lambda: env_int("CLAUDE_TIMEOUT_SECONDS", 600))
    claude_pricing_json: str = field(default_factory=lambda: env_str("CLAUDE_PRICING_JSON"))

    # --- Prompt caching -----------------------------------------------------
    prompt_cache_agents: str = field(default_factory=lambda: env_str("PROMPT_CACHE_AGENTS", "automation"))
    prompt_cache_ttl: str = field(default_factory=lambda: env_str("PROMPT_CACHE_TTL", "5m"))  # "5m" | "1h"
    spec_repair_attempts: int = field(default_factory=lambda: env_int("SPEC_REPAIR_ATTEMPTS", 2))

    # --- Local LLM (Ollama) -------------------------------------------------
    ollama_base_url: str = field(default_factory=lambda: env_str("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    ollama_model: str = field(default_factory=lambda: env_str("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    ollama_timeout_s: int = field(default_factory=lambda: env_int("OLLAMA_TIMEOUT_SECONDS", 300))
    ollama_triage_model: str = field(default_factory=lambda: env_str("OLLAMA_TRIAGE_MODEL"))
    ollama_architect_model: str = field(default_factory=lambda: env_str("OLLAMA_ARCHITECT_MODEL"))
    ollama_automation_model: str = field(default_factory=lambda: env_str("OLLAMA_AUTOMATION_MODEL"))
    ollama_max_tokens: int = field(default_factory=lambda: env_int("OLLAMA_MAX_TOKENS", 4096))
    ollama_max_test_cases: int = field(default_factory=lambda: env_int("OLLAMA_MAX_TEST_CASES", 5))

    # --- Skills (skills/*/SKILL.md appended to agent system prompts) --------
    skills_enabled: bool = field(default_factory=lambda: env_bool("SKILLS_ENABLED", True))
    skills_dir: str = field(default_factory=lambda: env_str("SKILLS_DIR", "skills"))
    # Off by default on local models: a small context window fills fast, and the
    # Ollama setup was tuned without them.
    skills_on_local: bool = field(default_factory=lambda: env_bool("SKILLS_ON_LOCAL", False))
    skills_max_chars: int = field(default_factory=lambda: env_int("SKILLS_MAX_CHARS", 12_000))

    # --- TypeScript test repository ----------------------------------------
    github_token: str = field(default_factory=lambda: env_str("GITHUB_TOKEN"))
    test_repo_url: str = field(default_factory=lambda: env_str("TEST_REPO_URL"))
    test_repo_local_path: str = field(default_factory=lambda: env_str("TEST_REPO_LOCAL_PATH"))
    test_repo_base_branch: str = field(default_factory=lambda: env_str("TEST_REPO_BASE_BRANCH", "main"))
    test_spec_dir: str = field(default_factory=lambda: env_str("TEST_SPEC_DIR", "tests/qa"))
    git_push_enabled: bool = field(default_factory=lambda: env_bool("GIT_PUSH_ENABLED", True))
    git_author_name: str = field(default_factory=lambda: env_str("GIT_AUTHOR_NAME", "QA Agent"))
    git_author_email: str = field(default_factory=lambda: env_str("GIT_AUTHOR_EMAIL", "qa-agent@users.noreply.github.com"))
    git_timeout_s: int = field(default_factory=lambda: env_int("GIT_TIMEOUT_SECONDS", 120))
    # Write the spec and run it IN PLACE: no fetch, no branch, no commit, no push.
    no_git: bool = field(default_factory=lambda: env_bool("TEST_REPO_NO_GIT", False))
    # Optional .env-style file whose variables are passed to Playwright (e.g. the test
    # login). Lets the agent's own clone run without a copy of anyone's secrets in it.
    test_repo_env_file: str = field(default_factory=lambda: env_str("TEST_REPO_ENV_FILE"))

    # --- Package manager / Playwright execution -----------------------------
    pnpm_install_timeout_s: int = field(default_factory=lambda: env_int("PNPM_INSTALL_TIMEOUT_SECONDS", 600))
    playwright_timeout_s: int = field(default_factory=lambda: env_int("PLAYWRIGHT_TIMEOUT_SECONDS", 1800))
    playwright_install_browsers: bool = field(default_factory=lambda: env_bool("PLAYWRIGHT_INSTALL_BROWSERS", False))
    playwright_extra_args: str = field(default_factory=lambda: env_str("PLAYWRIGHT_EXTRA_ARGS", "--reporter=line"))
    package_manager: str = field(default_factory=lambda: env_str("PACKAGE_MANAGER", "pnpm").lower())
    skip_dependency_install: bool = field(default_factory=lambda: env_bool("SKIP_DEPENDENCY_INSTALL", False))
    # Markdown file of REAL roles/names captured from the app under test.
    page_contract_path: str = field(default_factory=lambda: env_str("PAGE_CONTRACT_PATH"))
    # Live accessibility snapshot of the app, taken before the spec is written
    # (taf/steps/ui_snapshot.py). The recipe defaults to the page contract's
    # `<name>.snapshot.json`; the step skips itself when the recipe or the saved
    # session is missing.
    live_snapshot_enabled: bool = field(default_factory=lambda: env_bool("LIVE_SNAPSHOT_ENABLED", True))
    live_snapshot_recipe: str = field(default_factory=lambda: env_str("LIVE_SNAPSHOT_RECIPE"))
    live_snapshot_max_chars: int = field(default_factory=lambda: env_int("LIVE_SNAPSHOT_MAX_CHARS", 12_000))
    live_snapshot_timeout_s: int = field(default_factory=lambda: env_int("LIVE_SNAPSHOT_TIMEOUT_SECONDS", 420))
    # Repairs driven by the Playwright run itself (taf/steps/run_repair.py): failing
    # tests go back to the Automation Engineer with their error and page snapshot.
    # 0 turns it off. A repair that weakens a test is rejected.
    run_repair_attempts: int = field(default_factory=lambda: env_int("RUN_REPAIR_ATTEMPTS", 2))

    # --- Workflow: checkpoints, review, approval, pull request ---------------
    # LangGraph checkpoints (one thread per run) so a run can resume after a crash or
    # an approval wait. A separate file: qa_runs.db keeps its schema untouched.
    qa_checkpoint_db_path: str = field(default_factory=lambda: env_str("QA_CHECKPOINT_DB_PATH", "qa_checkpoints.db"))
    # Pause before anything leaves the machine (push / pull request) until a person approves.
    require_approval: bool = field(default_factory=lambda: env_bool("REQUIRE_APPROVAL", True))
    # Open a pull request in the test repository after an approved push. Needs GITHUB_TOKEN
    # (pull requests: read and write) or a signed-in GitHub CLI (gh).
    pr_create_enabled: bool = field(default_factory=lambda: env_bool("PR_CREATE_ENABLED", False))
    # Optional LLM review of the generated spec (conventions, maintainability). Off by default.
    review_with_llm: bool = field(default_factory=lambda: env_bool("REVIEW_WITH_LLM", False))
    claude_review_model: str = field(default_factory=lambda: env_str("CLAUDE_REVIEW_MODEL", "claude-sonnet-5-5"))
    claude_review_effort: str = field(default_factory=lambda: env_str("CLAUDE_REVIEW_EFFORT", "medium"))
    claude_review_max_tokens: int = field(default_factory=lambda: env_int("CLAUDE_REVIEW_MAX_TOKENS", 16_000))
    ollama_review_model: str = field(default_factory=lambda: env_str("OLLAMA_REVIEW_MODEL"))
    # How much of the test repository's conventions (config, fixtures, page objects) the
    # Automation Engineer is shown.
    repo_context_max_chars: int = field(default_factory=lambda: env_int("REPO_CONTEXT_MAX_CHARS", 14_000))

    # --- Langfuse (optional LLM observability) --------------------------------
    langfuse_public_key: str = field(default_factory=lambda: env_str("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: str = field(default_factory=lambda: env_str("LANGFUSE_SECRET_KEY"))
    langfuse_base_url: str = field(default_factory=lambda: env_str("LANGFUSE_BASE_URL", env_str("LANGFUSE_HOST")))
    langfuse_enabled: bool = field(default_factory=lambda: env_bool("LANGFUSE_ENABLED", True))
    langfuse_environment: str = field(default_factory=lambda: env_str("LANGFUSE_ENVIRONMENT", "local"))
    # metadata = no prompt/response text; truncated = text cut to LANGFUSE_MAX_FIELD_CHARS; full.
    langfuse_capture: str = field(default_factory=lambda: env_str("LANGFUSE_CAPTURE", "truncated").lower())
    langfuse_max_field_chars: int = field(default_factory=lambda: env_int("LANGFUSE_MAX_FIELD_CHARS", 4000))
    langfuse_timeout_s: int = field(default_factory=lambda: env_int("LANGFUSE_TIMEOUT_SECONDS", 5))
    langfuse_flush_timeout_s: int = field(default_factory=lambda: env_int("LANGFUSE_FLUSH_TIMEOUT_SECONDS", 10))

    # --- Budget / Paperclip Control Room -----------------------------------
    max_cloud_tokens_per_run: int = field(default_factory=lambda: env_int("MAX_CLOUD_TOKENS_PER_RUN", 250_000))
    max_cloud_tokens_per_day: int = field(default_factory=lambda: env_int("MAX_CLOUD_TOKENS_PER_DAY", 2_000_000))
    paperclip_api_url: str = field(default_factory=lambda: env_str("PAPERCLIP_API_URL").rstrip("/"))
    paperclip_api_key: str = field(default_factory=lambda: env_str("PAPERCLIP_API_KEY"))
    paperclip_project: str = field(default_factory=lambda: env_str("PAPERCLIP_PROJECT"))
    paperclip_enforce_budget: bool = field(default_factory=lambda: env_bool("PAPERCLIP_ENFORCE_BUDGET", True))

    # --- Xray ---------------------------------------------------------------
    xray_project_key: str = field(default_factory=lambda: env_str("XRAY_PROJECT_KEY"))
    xray_test_folder: str = field(default_factory=lambda: env_str("XRAY_TEST_FOLDER", "AI QA/{ticket_id}"))
    xray_requirement_link_type: str = field(default_factory=lambda: env_str("XRAY_REQUIREMENT_LINK_TYPE", "Test"))
    xray_include_priority: bool = field(default_factory=lambda: env_bool("XRAY_INCLUDE_PRIORITY", False))
    # Folder in the TEST repo where each ticket's Xray test cases are written
    # (xray/<TICKET>/...), committed next to the spec for review before any export.
    xray_dir: str = field(default_factory=lambda: env_str("XRAY_DIR", "xray"))
    # Only for `taf xray export --apply` (Xray Cloud > Settings > API Keys).
    xray_client_id: str = field(default_factory=lambda: env_str("XRAY_CLIENT_ID"))
    xray_client_secret: str = field(default_factory=lambda: env_str("XRAY_CLIENT_SECRET"))
    xray_base_url: str = field(default_factory=lambda: env_str("XRAY_BASE_URL", "https://xray.cloud.getxray.app").rstrip("/"))

    # --- Ticket source ------------------------------------------------------
    # jira = the real flow: tickets and their linked PRs come from the Jira API.
    # seed = demo flow: data/seed_tickets.json, works with no credentials.
    # auto = jira when JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN are all set, else seed.
    ticket_source: str = field(default_factory=lambda: env_str("TICKET_SOURCE", "auto").lower())

    # --- Source repository (where the PR diffs come from) -------------------
    # PR numbers come from Jira's development panel (or `pull_requests` in a seed
    # ticket); the diffs are read with git from a blobless bare clone kept at
    # SOURCE_REPO_LOCAL_PATH, using your normal git sign-in. No GitHub token needed.
    source_repo_url: str = field(default_factory=lambda: env_str("SOURCE_REPO_URL"))
    source_repo_local_path: str = field(default_factory=lambda: env_str("SOURCE_REPO_LOCAL_PATH", "../tripbuilder-src.git"))
    source_base_branch: str = field(default_factory=lambda: env_str("SOURCE_BASE_BRANCH", "develop"))
    fetch_pr_diffs: bool = field(default_factory=lambda: env_bool("FETCH_PR_DIFFS", True))
    # Ranking of a large diff before it reaches a model: files under these path
    # prefixes come first; files matching the exclude globs are listed but never shown.
    diff_focus_paths: str = field(default_factory=lambda: env_str("DIFF_FOCUS_PATHS", "apps/"))
    diff_exclude_globs: str = field(default_factory=lambda: env_str(
        "DIFF_EXCLUDE_GLOBS",
        "*.lock,*package-lock.json,*pnpm-lock.yaml,*.md,*.snap,*.svg,*.png,*.jpg,*.jpeg,*.gif,*.ico,*.min.js,*.map"))
    # Local models get a smaller diff budget than MAX_DIFF_CHARS (a 4k context fills fast).
    ollama_max_diff_chars: int = field(default_factory=lambda: env_int("OLLAMA_MAX_DIFF_CHARS", 6000))

    # --- Dashboard / run history / seed -------------------------------------
    qa_db_path: str = field(default_factory=lambda: env_str("QA_DB_PATH", "qa_runs.db"))
    dashboard_target_url: str = field(default_factory=lambda: env_str("DASHBOARD_TARGET_URL"))
    seed_tickets_path: str = field(default_factory=lambda: env_str("SEED_TICKETS_PATH", "data/seed_tickets.json"))

    # --- Service ------------------------------------------------------------
    qa_service_api_key: str = field(default_factory=lambda: env_str("QA_SERVICE_API_KEY"))
    host: str = field(default_factory=lambda: env_str("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: env_int("PORT", 8000))
    max_diff_chars: int = field(default_factory=lambda: env_int("MAX_DIFF_CHARS", 120_000))
    max_env_log_chars: int = field(default_factory=lambda: env_int("MAX_ENV_LOG_CHARS", 40_000))
    max_exec_log_chars_for_llm: int = field(default_factory=lambda: env_int("MAX_EXEC_LOG_CHARS_FOR_LLM", 60_000))
    callback_timeout_s: int = field(default_factory=lambda: env_int("CALLBACK_TIMEOUT_SECONDS", 30))
    log_level: str = field(default_factory=lambda: env_str("LOG_LEVEL", "INFO"))

    @property
    def secrets(self) -> List[str]:
        """Values that must never appear in logs, reports or subprocess output."""
        candidates = (self.github_token, self.anthropic_api_key, self.paperclip_api_key,
                      self.xray_client_secret, self.langfuse_secret_key, os.getenv("JIRA_API_TOKEN", "").strip())
        return [s for s in candidates if s and len(s) >= 8]


TICKET_SOURCES = ("auto", "jira", "seed")

SETTINGS = Settings()
if SETTINGS.llm_provider not in LLM_PROVIDERS:
    raise SystemExit(f"LLM_PROVIDER must be one of {', '.join(LLM_PROVIDERS)}; got {SETTINGS.llm_provider!r}")
if SETTINGS.ticket_source not in TICKET_SOURCES:
    raise SystemExit(f"TICKET_SOURCE must be one of {', '.join(TICKET_SOURCES)}; got {SETTINGS.ticket_source!r}")


# =============================================================================
# Logging (with secret redaction)
# =============================================================================


class _RedactSecretsFilter(logging.Filter):
    """Scrubs known secrets from every log record so tokens never land in log files."""

    def __init__(self, secrets: List[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            for secret in self._secrets:
                message = message.replace(secret, "***REDACTED***")
            record.msg, record.args = message, None
        return True


def redact(text: str) -> str:
    """Remove configured secrets from arbitrary text (subprocess output, error strings, ...)."""
    for secret in SETTINGS.secrets:
        text = text.replace(secret, "***REDACTED***")
    return text


logging.basicConfig(
    level=getattr(logging, SETTINGS.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactSecretsFilter(SETTINGS.secrets))

log = logging.getLogger("qa")

"""
Pull requests in the test repository - find or open, never anything else.

Two transports, picked once:
  * GitHub REST API when GITHUB_TOKEN is set (a fine-grained token with
    "Pull requests: read and write" on the test repository),
  * otherwise the GitHub CLI (`gh`) with the machine's own sign-in.

`ensure_pull_request` looks for an open PR for the branch before creating one, and
treats GitHub's "already exists" answer as success, so a resumed run never opens a
second PR. Creation is not retried blindly: a timed-out POST may have succeeded.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from taf.config import PROJECT_ROOT, SETTINGS, Settings, redact
from taf.integrations.github.pull_requests import repo_slug
from taf.integrations.github.repository import run_command

log = logging.getLogger("qa.pr")

API = "https://api.github.com"


class PullRequestError(RuntimeError):
    """The pull request could not be found or created."""


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    url: str
    created: bool  # False when an open PR for the branch already existed
    draft: bool = False


class PullRequestClient:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self.settings = settings
        self.slug = repo_slug(settings.test_repo_url)

    @property
    def transport(self) -> str:
        if self.settings.github_token:
            return "api"
        return "gh" if shutil.which("gh") else ""

    def _require(self) -> None:
        if not self.slug:
            raise PullRequestError("TEST_REPO_URL is not a github.com repository URL.")
        if not self.transport:
            raise PullRequestError("PR_CREATE_ENABLED=true needs GITHUB_TOKEN or a signed-in GitHub CLI (gh).")

    # ------------------------------------------------------------------ API --
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.github_token}",
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def _api(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            return requests.request(method, f"{API}{path}", headers=self._headers(),
                                    timeout=self.settings.git_timeout_s, **kwargs)
        except requests.RequestException as exc:
            raise PullRequestError(f"GitHub API {method} {path} failed: {redact(str(exc))}") from exc

    def find_open(self, branch: str) -> Optional[PullRequestRef]:
        self._require()
        owner = self.slug.split("/")[0]
        if self.transport == "api":
            resp = self._api("GET", f"/repos/{self.slug}/pulls",
                             params={"head": f"{owner}:{branch}", "state": "open", "per_page": 5})
            if resp.status_code != 200:
                raise PullRequestError(f"GitHub API listing PRs returned {resp.status_code}: {resp.text[:300]}")
            items = resp.json()
            return (PullRequestRef(items[0]["number"], items[0]["html_url"], False, bool(items[0].get("draft")))
                    if items else None)
        result = run_command(["gh", "pr", "list", "--repo", self.slug, "--head", branch, "--state", "open",
                              "--json", "number,url,isDraft", "--limit", "5"],
                             cwd=PROJECT_ROOT, timeout_s=self.settings.git_timeout_s)
        if not result.ok:
            raise PullRequestError(f"gh pr list failed: {result.stderr.strip()[:300]}")
        items = json.loads(result.stdout or "[]")
        return PullRequestRef(items[0]["number"], items[0]["url"], False, bool(items[0].get("isDraft"))) if items else None

    def create(self, branch: str, title: str, body: str, draft: bool) -> PullRequestRef:
        self._require()
        base = self.settings.test_repo_base_branch
        if self.transport == "api":
            resp = self._api("POST", f"/repos/{self.slug}/pulls",
                             json={"title": title, "head": branch, "base": base, "body": body, "draft": draft})
            if resp.status_code == 201:
                data = resp.json()
                return PullRequestRef(data["number"], data["html_url"], True, draft)
            if resp.status_code == 422 and "already exists" in resp.text:
                existing = self.find_open(branch)
                if existing:
                    return existing
            raise PullRequestError(f"GitHub API creating the PR returned {resp.status_code}: {resp.text[:300]}")
        args = ["gh", "pr", "create", "--repo", self.slug, "--head", branch, "--base", base,
                "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        result = run_command(args, cwd=PROJECT_ROOT, timeout_s=self.settings.git_timeout_s)
        if not result.ok:
            existing = self.find_open(branch)
            if existing:
                return existing
            raise PullRequestError(f"gh pr create failed: {result.stderr.strip()[:300]}")
        url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() else 0
        return PullRequestRef(number, url, True, draft)

    def ensure_pull_request(self, branch: str, title: str, body: str, draft: bool) -> PullRequestRef:
        """The open PR for `branch`, created only if there is none."""
        existing = self.find_open(branch)
        if existing:
            log.info("PR already open for %s: %s", branch, existing.url)
            return existing
        return self.create(branch, title, body, draft)


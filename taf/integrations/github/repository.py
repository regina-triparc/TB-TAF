"""
The Playwright test repository: git (clone / branch / commit / push), dependency
install and Playwright execution, all through argument lists - never a shell.

In-place mode (TEST_REPO_NO_GIT=true) skips every git operation, so a person's
working copy can be used safely. See README "TypeScript test repository".
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from taf.config import SETTINGS, Settings, project_path, redact
from taf.core.text import strip_ansi

log = logging.getLogger("qa.repo")


class CommandError(RuntimeError):
    """A local CLI command failed (non-zero exit, timeout, or missing binary)."""

    def __init__(self, message: str, result: Optional["CommandResult"] = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_log(self) -> str:
        """Human-readable log block used in execution_logs."""
        header = f"$ {' '.join(self.args)}\n# exit={self.returncode} duration={self.duration_s:.1f}s"
        if self.timed_out:
            header += " TIMED OUT"
        body = ""
        if self.stdout.strip():
            body += f"\n--- stdout ---\n{self.stdout.rstrip()}"
        if self.stderr.strip():
            body += f"\n--- stderr ---\n{self.stderr.rstrip()}"
        return header + body


def _decode(stream: Any) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


def run_command(
    args: List[str],
    cwd: Path,
    timeout_s: int,
    env: Optional[Dict[str, str]] = None,
    check: bool = False,
) -> CommandResult:
    """
    Run a CLI command without a shell (no injection surface), capture stdout/stderr,
    strip ANSI codes and redact secrets. Never raises for a non-zero exit unless
    `check=True`; always raises CommandError for a missing binary.
    """
    started = time.monotonic()
    merged_env = {**os.environ, **(env or {})}
    # Resolve the executable through PATH/PATHEXT so Windows shims such as
    # `pnpm.cmd` are found without resorting to shell=True.
    resolved = shutil.which(args[0], path=merged_env.get("PATH"))
    if resolved:
        args = [resolved, *args[1:]]
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=merged_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        result = CommandResult(
            args=args,
            returncode=proc.returncode,
            stdout=redact(strip_ansi(proc.stdout or "")),
            stderr=redact(strip_ansi(proc.stderr or "")),
            duration_s=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            args=args,
            returncode=-1,
            stdout=redact(strip_ansi(_decode(exc.stdout))),
            stderr=redact(strip_ansi(_decode(exc.stderr))) + f"\n[process killed after {timeout_s}s timeout]",
            duration_s=time.monotonic() - started,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Executable not found: {args[0]!r} ({exc})") from exc
    except OSError as exc:
        raise CommandError(f"Could not start {args[0]!r}: {exc}") from exc

    log.debug("cmd=%s exit=%s %.1fs", " ".join(args), result.returncode, result.duration_s)
    if check and not result.ok:
        raise CommandError(f"Command failed: {' '.join(args)} (exit {result.returncode})", result)
    return result


class TestRepository:
    """
    Wraps the local clone of the TypeScript Playwright repository.

    Authentication: GITHUB_TOKEN is injected per-command through git's
    GIT_CONFIG_COUNT/KEY/VALUE environment variables as an HTTP Authorization
    header. The token therefore never appears in the remote URL, `.git/config`,
    the process argument list, or logs.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = project_path(settings.test_repo_local_path).resolve() if settings.test_repo_local_path else None

    # ------------------------------------------------------------------ git --
    def _git_env(self) -> Dict[str, str]:
        env = {
            "GIT_TERMINAL_PROMPT": "0",  # never hang waiting for a username/password prompt
            "GCM_INTERACTIVE": "never",  # Git Credential Manager (Windows/macOS): no login pop-ups
            "GIT_AUTHOR_NAME": self.settings.git_author_name,
            "GIT_AUTHOR_EMAIL": self.settings.git_author_email,
            "GIT_COMMITTER_NAME": self.settings.git_author_name,
            "GIT_COMMITTER_EMAIL": self.settings.git_author_email,
        }
        token = self.settings.github_token
        host = urlparse(self.settings.test_repo_url).hostname if self.settings.test_repo_url else "github.com"
        if token and host:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
                }
            )
        return env

    def git(self, *args: str, check: bool = True, cwd: Optional[Path] = None) -> CommandResult:
        return run_command(
            ["git", *args],
            cwd=cwd or self.path,  # type: ignore[arg-type]
            timeout_s=self.settings.git_timeout_s,
            env=self._git_env(),
            check=check,
        )

    def ensure_ready(self, logs: List[str]) -> None:
        """Validate the local clone, cloning it from TEST_REPO_URL on first use."""
        if self.path is None:
            raise CommandError("TEST_REPO_LOCAL_PATH is not configured.")
        if shutil.which("git") is None:
            raise CommandError("git is not installed or not on PATH.")

        if self.settings.no_git:
            # In-place mode: the directory is someone's working copy. Do not clone,
            # do not inspect its git state, do not touch its branches.
            if not self.path.is_dir():
                raise CommandError(f"TEST_REPO_LOCAL_PATH does not exist: {self.path}")
            logs.append(f"[info] In-place mode (TEST_REPO_NO_GIT=true): using {self.path} "
                        f"without any git operations.")
            return
        if (self.path / ".git").is_dir():
            return
        if self.path.exists() and any(self.path.iterdir()):
            raise CommandError(f"{self.path} exists, is not empty, and is not a git repository.")
        if not self.settings.test_repo_url:
            raise CommandError(f"{self.path} is not a git clone and TEST_REPO_URL is not set to clone it.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(
            ["git", "clone", self.settings.test_repo_url, str(self.path)],
            cwd=self.path.parent,
            timeout_s=max(self.settings.git_timeout_s, 600),
            env=self._git_env(),
        )
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError("git clone failed", result)

    def prepare_branch(self, ticket_id: str, logs: List[str]) -> str:
        """
        Sync the base branch and create (or reset) `qa/{ticket_id}` from it.

        Remote problems (fetch/pull) are logged and tolerated so offline runs still work;
        a failed checkout raises CommandError because the tree would be in an unknown state.
        """
        assert self.path is not None
        if self.settings.no_git:
            logs.append("[info] In-place mode: no branch created; the spec is written "
                        "onto the current working tree and left uncommitted for review.")
            return ""
        base = self.settings.test_repo_base_branch
        branch = f"qa/{ticket_id}"

        result = self.git("fetch", "origin", "--prune", check=False)
        logs.append(result.as_log())
        if not result.ok:
            logs.append("[warn] git fetch failed - continuing with the local state of the repository.")

        result = self.git("checkout", base, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError(
                f"Cannot check out base branch '{base}'. Is the working tree dirty? "
                "Use a dedicated clone for the QA agent.",
                result,
            )
        result = self.git("pull", "--ff-only", "origin", base, check=False)
        logs.append(result.as_log())
        if not result.ok:
            logs.append(f"[warn] git pull --ff-only origin {base} failed - using local '{base}'.")

        # `-B` is the idempotent form of `checkout -b`: re-runs of a ticket reset its branch.
        result = self.git("checkout", "-B", branch, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError(f"git checkout -B {branch} failed", result)
        return branch

    def write_spec(self, ticket_id: str, code: str, logs: List[str]) -> Path:
        """Write the spec file (confined to the repository) with LF line endings."""
        assert self.path is not None
        spec_dir = (self.path / self.settings.test_spec_dir).resolve()
        if self.path != spec_dir and self.path not in spec_dir.parents:
            raise CommandError(f"TEST_SPEC_DIR escapes the repository: {self.settings.test_spec_dir}")
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / f"{ticket_id}.spec.ts"
        spec_path.write_text(code, encoding="utf-8", newline="\n")  # LF on every OS
        logs.append(f"[info] wrote {spec_path.relative_to(self.path).as_posix()} ({len(code):,} bytes)")
        return spec_path

    def commit_and_push(self, ticket_id: str, spec_path: Path, branch: str, logs: List[str]) -> bool:
        """
        Commit the spec (skipped when identical to HEAD) and push the branch.
        Returns True if pushed. Commit failures raise; push failures are tolerated.
        """
        assert self.path is not None
        if self.settings.no_git:
            logs.append(f"[info] In-place mode: {spec_path.name} left uncommitted in your "
                        f"working copy. Nothing was staged, committed or pushed.")
            return False
        rel_spec = spec_path.relative_to(self.path).as_posix()
        result = self.git("add", "--", rel_spec, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError("git add failed", result)

        staged = self.git("diff", "--cached", "--quiet", "--", rel_spec, check=False)
        if staged.returncode == 0:
            logs.append("[info] spec unchanged since last commit - nothing to commit.")
        else:
            result = self.git(
                "commit", "-m", f"test({ticket_id}): add AI-generated Playwright spec",
                "-m", "Generated by the autonomous QA multi-agent framework.",
                check=False,
            )
            logs.append(result.as_log())
            if not result.ok:
                raise CommandError("git commit failed", result)

        # A rejected push of our own qa/* branch is retried with --force-with-lease,
        # which only overwrites what we last fetched.
        if not self.settings.git_push_enabled:
            logs.append("[info] GIT_PUSH_ENABLED=false - skipping git push.")
            return False
        result = self.git("push", "-u", "origin", branch, check=False)
        logs.append(result.as_log())
        if not result.ok and ("rejected" in result.stderr or "non-fast-forward" in result.stderr):
            result = self.git("push", "--force-with-lease", "-u", "origin", branch, check=False)
            logs.append(result.as_log())
        if not result.ok:
            logs.append("[warn] git push failed - spec is committed locally only. Check GITHUB_TOKEN scopes.")
        return result.ok

    # ----------------------------------------------------------- pnpm / PW --
    def _pw_env(self, target_url: str) -> Dict[str, str]:
        env = {
            "CI": "1",               # Playwright: no interactive HTML report server, retries per config
            "FORCE_COLOR": "0",
            "NO_COLOR": "1",
        }
        # Only set when non-empty: an exported BASE_URL="" defeats a config that does
        # `process.env.BASE_URL ?? FALLBACK`, because ?? only catches null/undefined.
        if target_url:
            env["BASE_URL"] = target_url
            env["PLAYWRIGHT_BASE_URL"] = target_url
        return env

    @property
    def _pm(self) -> str:
        return "npm" if self.settings.package_manager == "npm" else "pnpm"

    def _exec(self, *args: str) -> List[str]:
        """`pnpm exec <args>` / `npm exec -- <args>` - the two CLIs differ here."""
        return [self._pm, "exec", "--", *args] if self._pm == "npm" else [self._pm, "exec", *args]

    def install_dependencies(self, target_url: str, logs: List[str]) -> bool:
        """Install deps (+ optional browsers). Returns False if tests cannot run."""
        assert self.path is not None
        pm = self._pm
        if shutil.which(pm) is None:
            logs.append(f"[error] {pm} is not installed or not on PATH.")
            return False
        env = self._pw_env(target_url)
        if self.settings.skip_dependency_install:
            if not (self.path / "node_modules").is_dir():
                logs.append("[error] SKIP_DEPENDENCY_INSTALL is set but node_modules is missing.")
                return False
            logs.append("[info] Skipping dependency install (SKIP_DEPENDENCY_INSTALL=true); "
                        "using the existing node_modules.")
            return True
        install = run_command([pm, "install"], cwd=self.path, timeout_s=self.settings.pnpm_install_timeout_s, env=env)
        logs.append(install.as_log())
        if not install.ok:
            logs.append(f"[error] {pm} install failed - tests were not executed.")
            return False
        if self.settings.playwright_install_browsers:
            browsers = run_command(
                self._exec("playwright", "install", "--with-deps", "chromium"),
                cwd=self.path, timeout_s=self.settings.pnpm_install_timeout_s, env=env,
            )
            logs.append(browsers.as_log())
        return True

    def list_tests(self, spec_path: Path, target_url: str) -> CommandResult:
        """
        `playwright test <spec> --list`: loads and transpiles the spec and collects its
        tests without launching a browser. Syntax errors, bad imports and a spec with
        no tests all fail here in seconds - the signal for the repair loop.
        """
        assert self.path is not None
        rel_spec = spec_path.relative_to(self.path).as_posix()
        return run_command(
            self._exec("playwright", "test", rel_spec, "--list"),
            cwd=self.path, timeout_s=120, env=self._pw_env(target_url),
        )

    def run_playwright(self, spec_path: Path, target_url: str, logs: List[str]) -> bool:
        """`pnpm exec playwright test <spec>`. Returns True only if all tests pass."""
        assert self.path is not None
        rel_spec = spec_path.relative_to(self.path).as_posix()
        extra = self.settings.playwright_extra_args.split() if self.settings.playwright_extra_args else []
        test = run_command(
            self._exec("playwright", "test", rel_spec, *extra),
            cwd=self.path, timeout_s=self.settings.playwright_timeout_s, env=self._pw_env(target_url),
        )
        logs.append(test.as_log())
        return test.ok


TEST_REPO = TestRepository(SETTINGS)

# The local clone is a single shared working tree: git checkout + test execution for
# one ticket must never interleave with another ticket's run.
REPO_LOCK = threading.Lock()

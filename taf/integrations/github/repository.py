"""
The Playwright test repository: git (clone / branch / commit / push), dependency
install and Playwright execution, all through argument lists - never a shell.

In-place mode (TEST_REPO_NO_GIT=true) skips every git operation, so a person's
working copy can be used safely. See README "TypeScript test repository".
"""
from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import dotenv_values

from taf.config import PROJECT_ROOT, SETTINGS, Settings, project_path, redact
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


def _slug(remote_url: str) -> str:
    """'https://github.com/Owner/Repo.git' or 'git@github.com:Owner/Repo' -> 'owner/repo'."""
    match = re.search(r"[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", (remote_url or "").strip())
    return match.group(1).lower() if match else ""


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

    def assert_outside_orchestrator(self) -> None:
        """
        Refuse a test repository that is, contains, or shares a remote with this project.

        Generated tests belong in the separate Playwright repository only; a mistyped
        TEST_REPO_LOCAL_PATH must never make the agent write into (or commit to) the
        orchestrator's own checkout.
        """
        if self.path is None:
            return
        own = PROJECT_ROOT.resolve()
        target = self.path.resolve()
        if target == own or own in target.parents or target in own.parents:
            raise CommandError(f"TEST_REPO_LOCAL_PATH ({target}) overlaps this project ({own}). "
                               "Point it at a separate clone of the Playwright test repository.")
        if not (target / ".git").exists() or not (own / ".git").exists():
            return
        own_remote = run_command(["git", "remote", "get-url", "origin"], cwd=own, timeout_s=30)
        test_remote = run_command(["git", "remote", "get-url", "origin"], cwd=target, timeout_s=30)
        own_slug, test_slug = _slug(own_remote.stdout), _slug(test_remote.stdout)
        if own_remote.ok and test_remote.ok and own_slug and own_slug == test_slug:
            raise CommandError(f"The test repository's origin ({test_slug}) is this project's own repository.")

    def ensure_ready(self, logs: List[str]) -> None:
        """Validate the local clone, cloning it from TEST_REPO_URL on first use."""
        if self.path is None:
            raise CommandError("TEST_REPO_LOCAL_PATH is not configured.")
        if shutil.which("git") is None:
            raise CommandError("git is not installed or not on PATH.")
        self.assert_outside_orchestrator()

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

    def sync_base(self, logs: List[str], fetch: bool = True) -> None:
        """
        Check out the base branch, fast-forwarded to origin when `fetch` is set.

        Remote problems (fetch/pull) are logged and tolerated so offline runs still work;
        a failed checkout raises CommandError because the tree would be in an unknown state.
        """
        if self.settings.no_git:
            return
        base = self.settings.test_repo_base_branch
        if fetch:
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
        if fetch:
            result = self.git("pull", "--ff-only", "origin", base, check=False)
            logs.append(result.as_log())
            if not result.ok:
                logs.append(f"[warn] git pull --ff-only origin {base} failed - using local '{base}'.")

    def _ref_exists(self, ref: str) -> bool:
        return self.git("rev-parse", "--verify", "--quiet", ref, check=False).ok

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def prepare_branch(self, ticket_id: str, logs: List[str]) -> str:
        """
        Check out `qa/{ticket_id}` for publishing, without discarding anyone's commits.

        * The branch already exists on origin: continue on top of it, so a re-run adds a
          commit to the open pull request instead of rewriting it.
        * Otherwise it starts from the freshly pulled base branch.
        * A local branch with commits that are on no remote (an earlier run whose push
          failed) is kept as it is and built upon - never reset.
        """
        assert self.path is not None
        if self.settings.no_git:
            logs.append("[info] In-place mode: no branch created; files are written "
                        "onto the current working tree and left uncommitted for review.")
            return ""
        branch = f"qa/{ticket_id}"
        self.sync_base(logs, fetch=True)
        local, remote = f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"
        if self._ref_exists(local):
            unpushed = self.git("log", "--oneline", local, "--not", "--remotes", check=False).stdout.strip()
            if unpushed:
                result = self.git("checkout", branch, check=False)
                logs.append(result.as_log())
                if not result.ok:
                    raise CommandError(f"git checkout {branch} failed", result)
                logs.append(f"[warn] {branch} has local commits that were never pushed - building on top of them.")
                return branch
        # Safe: the local branch (if any) holds nothing that is not already on a remote.
        start = f"origin/{branch}" if self._ref_exists(remote) else self.settings.test_repo_base_branch
        result = self.git("checkout", "-B", branch, start, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError(f"git checkout -B {branch} {start} failed", result)
        return branch

    def _confined(self, rel_dir: str) -> Path:
        assert self.path is not None
        root = self.path.resolve()  # compare like with like (e.g. Windows 8.3 short names)
        target = (root / rel_dir).resolve()
        if root != target and root not in target.parents:
            raise CommandError(f"{rel_dir} escapes the repository")
        return target

    def write_spec(self, ticket_id: str, code: str, logs: List[str]) -> Path:
        """Write the spec file (confined to the repository) with LF line endings."""
        assert self.path is not None
        root = self.path.resolve()
        spec_dir = self._confined(self.settings.test_spec_dir)
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / f"{ticket_id}.spec.ts"
        spec_path.write_text(code, encoding="utf-8", newline="\n")  # LF on every OS
        logs.append(f"[info] wrote {spec_path.relative_to(root).as_posix()} ({len(code):,} bytes)")
        return spec_path

    def write_files(self, rel_dir: str, files: Dict[str, str], logs: List[str]) -> List[Path]:
        """Write text files into `rel_dir` (confined to the repository), LF line endings."""
        assert self.path is not None
        root = self.path.resolve()
        target = self._confined(rel_dir)
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for name, content in files.items():
            path = target / Path(name).name  # names only: no sub-paths from callers
            path.write_text(content, encoding="utf-8", newline="\n")
            written.append(path)
        if written:
            logs.append(f"[info] wrote {target.relative_to(root).as_posix()}/: "
                        + ", ".join(p.name for p in written))
        return written

    def write_ticket_files(self, ticket_id: str, code: str, xray_dir: str, xray_files: Dict[str, str],
                           logs: List[str]) -> Tuple[Optional[Path], List[Path]]:
        """A run's only outputs: TEST_SPEC_DIR/<TICKET>.spec.ts and XRAY_DIR/<TICKET>/*."""
        spec = self.write_spec(ticket_id, code, logs) if code else None
        xray_paths = self.write_files(xray_dir, xray_files, logs) if xray_files else []
        return spec, xray_paths

    def restore(self, paths: List[Path], logs: List[str]) -> None:
        """
        Undo `write_ticket_files` on the current checkout: tracked files go back to their
        committed version, files the run created are removed. Only these paths are touched.
        """
        if self.settings.no_git or self.path is None:
            return
        root = self.path.resolve()
        for path in paths:
            rel = path.resolve().relative_to(root).as_posix()
            if self.git("ls-files", "--error-unmatch", "--", rel, check=False).ok:
                self.git("checkout", "--", rel, check=False)
            else:
                path.unlink(missing_ok=True)
        if paths:
            logs.append(f"[info] removed this run's {len(paths)} file(s) from the shared working tree")

    @contextlib.contextmanager
    def ticket_files_on_base(self, ticket_id: str, code: str, xray_dir: str, xray_files: Dict[str, str],
                             logs: List[str]) -> Iterator[Tuple[Optional[Path], List[Path]]]:
        """
        The run's files written onto the base branch for validation, removed afterwards
        so the shared clone is clean for the next ticket. In-place mode leaves them.
        Callers hold REPO_LOCK.
        """
        self.sync_base(logs, fetch=False)
        spec, xray_paths = self.write_ticket_files(ticket_id, code, xray_dir, xray_files, logs)
        try:
            yield spec, xray_paths
        finally:
            self.restore(([spec] if spec else []) + xray_paths, logs)

    def commit(self, ticket_id: str, paths: List[Path], logs: List[str]) -> Tuple[str, bool]:
        """
        Commit exactly `paths` in one commit. Returns (HEAD sha, whether a commit was made):
        files identical to HEAD are not committed again, so a resumed publish is a no-op
        instead of a duplicate commit.
        """
        assert self.path is not None
        root = self.path.resolve()
        rel = [p.resolve().relative_to(root).as_posix() for p in paths]
        result = self.git("add", "--", *rel, check=False)
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError("git add failed", result)

        staged = self.git("diff", "--cached", "--quiet", "--", *rel, check=False)
        if staged.returncode == 0:
            logs.append("[info] spec and test cases unchanged since last commit - nothing to commit.")
            return self.head_sha(), False
        has_spec = any(p.suffix == ".ts" for p in paths)
        has_xray = any(".xray." in p.name for p in paths)
        what = " and ".join(w for w, on in (("Playwright spec", has_spec), ("Xray test cases", has_xray)) if on)
        result = self.git(
            "commit", "-m", f"test({ticket_id}): add AI-generated {what}",
            "-m", "Generated by the TB-TAF QA agent. Review before merging; Xray test cases are "
                  "exported to Jira from main with `python -m taf xray export`.",
            "--", *rel,
            check=False,
        )
        logs.append(result.as_log())
        if not result.ok:
            raise CommandError("git commit failed", result)
        return self.head_sha(), True

    def push(self, branch: str, logs: List[str]) -> bool:
        """
        Push `branch` to origin - never forced. Skipped when origin already has HEAD
        (a resumed run). A rejected push is reported, not overwritten: the branch moved
        on origin, and a person decides what happens next.
        """
        remote = f"refs/remotes/origin/{branch}"
        if self._ref_exists(remote) and self.git("merge-base", "--is-ancestor", "HEAD", remote, check=False).ok:
            logs.append(f"[info] origin/{branch} already contains HEAD - nothing to push.")
            return True
        result = self.git("push", "-u", "origin", branch, check=False)
        logs.append(result.as_log())
        if not result.ok:
            logs.append("[warn] git push failed - committed locally only. Check repo access or GITHUB_TOKEN; "
                        "a rejected push means the branch moved on origin and needs a person.")
        return result.ok

    # ----------------------------------------------------------- pnpm / PW --
    def _test_repo_env(self) -> Dict[str, str]:
        """Variables from TEST_REPO_ENV_FILE (never logged). Empty when unset or unreadable."""
        if not self.settings.test_repo_env_file:
            return {}
        path = project_path(self.settings.test_repo_env_file)
        if not path.is_file():
            log.warning("TEST_REPO_ENV_FILE not found: %s", path)
            return {}
        return {k: v for k, v in dotenv_values(path).items() if v}

    def _pw_env(self, target_url: str) -> Dict[str, str]:
        env = {
            **self._test_repo_env(),
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
            if (self.path / "node_modules").is_dir():
                logs.append("[info] Skipping dependency install (SKIP_DEPENDENCY_INSTALL=true); "
                            "using the existing node_modules.")
                return True
            # A clone the agent just made has no node_modules: install once, then later
            # runs take the skip above.
            logs.append("[info] node_modules missing - installing once despite SKIP_DEPENDENCY_INSTALL.")
        # `npm ci` installs exactly the lockfile and never rewrites it, so the agent's
        # clone stays clean for the next `git checkout` of the base branch.
        has_npm_lock = pm == "npm" and (self.path / "package-lock.json").is_file()
        install = run_command([pm, "ci"] if has_npm_lock else [pm, "install"],
                              cwd=self.path, timeout_s=self.settings.pnpm_install_timeout_s, env=env)
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
        rel_spec = spec_path.resolve().relative_to(self.path.resolve()).as_posix()
        return run_command(
            self._exec("playwright", "test", rel_spec, "--list"),
            cwd=self.path, timeout_s=120, env=self._pw_env(target_url),
        )

    def _local_bin(self, name: str) -> Optional[Path]:
        """A tool installed in the test repo's node_modules (never a global or downloaded one)."""
        assert self.path is not None
        bin_dir = self.path / "node_modules" / ".bin"
        return next((bin_dir / n for n in (name, f"{name}.cmd") if (bin_dir / n).is_file()), None)

    def tsconfig_for(self, spec_path: Path) -> Optional[Path]:
        """The nearest tsconfig.json at or above the spec, inside the repository."""
        assert self.path is not None
        root, folder = self.path.resolve(), spec_path.resolve().parent
        while folder == root or root in folder.parents:
            if (folder / "tsconfig.json").is_file():
                return folder / "tsconfig.json"
            folder = folder.parent
        return None

    def has_tool(self, name: str) -> bool:
        """tsc / eslint available in the repo (eslint also needs the repo's own config)."""
        assert self.path is not None
        if self._local_bin(name) is None:
            return False
        return name != "eslint" or any((self.path / n).is_file() for n in self.ESLINT_CONFIGS)

    def typecheck(self, spec_path: Path, target_url: str) -> Optional[CommandResult]:
        """`tsc --noEmit` on the spec's tsconfig. None when the repo has no TypeScript compiler installed."""
        assert self.path is not None
        config = self.tsconfig_for(spec_path)
        if self._local_bin("tsc") is None or config is None:
            return None
        rel = config.relative_to(self.path.resolve()).as_posix()
        return run_command(self._exec("tsc", "--noEmit", "--pretty", "false", "-p", rel),
                           cwd=self.path, timeout_s=300, env=self._pw_env(target_url))

    ESLINT_CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
                      ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml")

    def lint(self, paths: List[Path], target_url: str) -> Optional[CommandResult]:
        """The repo's own ESLint on the given files. None when the repo has no ESLint set up."""
        assert self.path is not None
        if not self.has_tool("eslint"):
            return None
        root = self.path.resolve()
        rel = [p.resolve().relative_to(root).as_posix() for p in paths]
        return run_command(self._exec("eslint", *rel), cwd=self.path, timeout_s=300, env=self._pw_env(target_url))

    def run_playwright(self, spec_path: Path, target_url: str, logs: List[str],
                       json_report: Optional[Path] = None, lines: Optional[List[int]] = None) -> bool:
        """
        `pnpm exec playwright test <spec>`. Returns True only if all tests pass.
        `json_report` also writes Playwright's JSON report there (per-test results);
        `lines` runs only the tests that start on those lines (`<spec>:<line>`). Locations,
        not a --grep pattern: on Windows npm.cmd runs through cmd.exe, which reads `|` as a pipe.
        """
        assert self.path is not None
        rel_spec = spec_path.resolve().relative_to(self.path.resolve()).as_posix()
        targets = [f"{rel_spec}:{n}" for n in sorted(set(lines))] if lines else [rel_spec]
        extra = self.settings.playwright_extra_args.split() if self.settings.playwright_extra_args else []
        env = self._pw_env(target_url)
        if json_report is not None:
            extra = with_json_reporter(extra)
            json_report.parent.mkdir(parents=True, exist_ok=True)
            json_report.unlink(missing_ok=True)
            env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = str(json_report)
        test = run_command(
            self._exec("playwright", "test", *targets, *extra),
            cwd=self.path, timeout_s=self.settings.playwright_timeout_s, env=env,
        )
        logs.append(test.as_log())
        return test.ok


def with_json_reporter(args: List[str]) -> List[str]:
    """Add Playwright's `json` reporter next to whichever reporter the arguments choose."""
    out, found = [], False
    for arg in args:
        if arg.startswith("--reporter="):
            found = True
            reporters = arg.split("=", 1)[1].split(",")
            if "json" not in reporters:
                arg = f"--reporter={','.join(reporters + ['json'])}"
        out.append(arg)
    return out if found else out + ["--reporter=line,json"]


TEST_REPO = TestRepository(SETTINGS)

# The local clone is a single shared working tree: git checkout + test execution for
# one ticket must never interleave with another ticket's run.
REPO_LOCK = threading.Lock()

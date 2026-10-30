"""
Tests a spec skips on an environment variable nobody defines.

The automation prompt allows `test.skip(!process.env.X, 'reason')` for credentials. A
model that cannot find test data in the page contract or the page objects reaches for
the same pattern and invents variables (`TB601_TRIP_ID`, `TB601_DAY_LABEL`): every
test then skips in half a second and the run looks green. A guard counts as invented
when the variable is not declared anywhere Playwright reads it from: the test
repository's `.env.example` or `.env`, TEST_REPO_ENV_FILE, or the process environment.
Such tests go to run repair, and a run that still has them does not pass.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from dotenv import dotenv_values

from taf.config import SETTINGS, project_path

_SKIP_RE = re.compile(r"\btest\.skip\s*\(")
_ENV_READ_RE = re.compile(r"process\.env(?:\.([A-Za-z_]\w*)|\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\])")
_STRING_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")
_ENV_CONST_RE =re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*([^;\n]*process\.env[^;\n]*)")
# Set by TAF itself for every run (TestRepository._pw_env).
_ALWAYS = {"BASE_URL", "PLAYWRIGHT_BASE_URL", "CI", "FORCE_COLOR", "NO_COLOR"}


def declared_env(repo: Optional[Path]) -> Set[str]:
    """Every variable a test run can see: declared in the repo's env files or set in the environment."""
    names = set(_ALWAYS) | {k for k, v in os.environ.items() if v}
    files = [repo / ".env.example", repo / ".env"] if repo is not None else []
    if SETTINGS.test_repo_env_file:
        files.append(project_path(SETTINGS.test_repo_env_file))
    for path in files:
        if path.is_file():
            names |= set(dotenv_values(path))
    return names


def _call_args(code: str, start: int) -> str:
    """The argument text of the call whose `(` is at `start - 1`, skipping over string literals."""
    depth, i, quote = 1, start, ""
    while i < len(code) and depth:
        ch = code[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return code[start:i - 1]


def _env_names(text: str) -> Set[str]:
    return {a or b for a, b in _ENV_READ_RE.findall(text)}


def _guarded(region: str, env_consts: Dict[str, Set[str]]) -> Set[str]:
    """Variables the `test.skip(...)` calls in `region` depend on, directly or through a constant."""
    names: Set[str] = set()
    for match in _SKIP_RE.finditer(region):
        # process.env['X'] -> process.env.X first, then drop strings: the reason text is not the condition.
        args = _ENV_READ_RE.sub(lambda m: f"process.env.{m.group(1) or m.group(2)}", _call_args(region, match.end()))
        args = _STRING_RE.sub("''", args)
        names |= _env_names(args)
        for const, env in env_consts.items():
            if re.search(rf"(?<![\w$.]){re.escape(const)}(?![\w$])", args):
                names |= env
    return names


def env_guards(code: str, bodies: Dict[str, str], declared: Set[str]) -> Dict[str, List[str]]:
    """
    `{TC: [undeclared variables]}` for every test a `test.skip` on an undeclared variable
    can skip. A guard outside the tests (describe body, beforeEach) covers all of them.
    """
    env_consts = {name: _env_names(expr) for name, expr in _ENV_CONST_RE.findall(code)}
    first = min((code.find(body) for body in bodies.values() if body), default=len(code))
    shared = _guarded(code[:first], env_consts) - declared
    out: Dict[str, List[str]] = {}
    for tc, body in bodies.items():
        names = shared | (_guarded(body, env_consts) - declared)
        if names:
            out[tc] = sorted(names)
    return out

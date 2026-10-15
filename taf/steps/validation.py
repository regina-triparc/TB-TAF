"""
Deterministic validation of a generated spec - the mandatory gate before anything
is published.

    static checks   the spec parses, has tests, imports only modules that exist in
                    the test repository (or its package.json), and uses no Node APIs
                    such as child_process / fs / eval
    repo checks     with the spec written onto the base branch of the test repo:
                    `playwright test --list` (it loads), `tsc --noEmit` and the repo's
                    ESLint - each only when the repo has that tool installed

A failure the model can fix (a type error, a bad import) is `retryable`: the graph
sends it back to the Automation Engineer as `spec_feedback`. A failure it cannot fix
(no test repository, dependencies missing) ends the attempt loop at once.
Unsafe code is never loaded: the repo checks only run when the static checks pass.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from taf.agents.automation.agent import automatable_cases, max_generations
from taf.agents.base import agent_node
from taf.agents.reporter.agent import parse_playwright_counts
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, CommandError, CommandResult
from taf.integrations.jira import xray
from taf.llm.router import ROUTER
from taf.steps.context import append_log, repo_available

Check = Dict[str, Any]

_IMPORT_RE = re.compile(r"""(?:\bimport\s+(?:type\s+)?(?:[\w*{}\s,]+?\s+from\s+)?|\brequire\(\s*|\bimport\(\s*)['"]([^'"]+)['"]""")
_NODE_BUILTINS = {"child_process", "fs", "fs/promises", "net", "http", "https", "os", "vm", "worker_threads",
                  "cluster", "dgram", "dns", "tls", "process", "module", "inspector"}
_DANGEROUS = [
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bnew\s+Function\s*\("), "new Function()"),
    (re.compile(r"process\.env\.\w+\s*=(?!=)"), "assigns to process.env"),
]
_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".mjs", "/index.ts", "/index.js", ".d.ts")


def check(name: str, ok: bool, detail: str = "", retryable: bool = True, skipped: bool = False) -> Check:
    return {"name": name, "ok": bool(ok), "detail": detail, "retryable": retryable, "skipped": skipped}


def imports(code: str) -> List[str]:
    return list(dict.fromkeys(_IMPORT_RE.findall(code)))


def _package_name(specifier: str) -> str:
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") else parts[0]


def package_names(repo_root: Optional[Path]) -> Set[str]:
    names = {"@playwright/test"}
    if repo_root is None:
        return names
    try:
        package = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names |= set((package.get(key) or {}).keys())
    return names


def code_checks(code: str, spec_rel: str, repo_root: Optional[Path]) -> List[Check]:
    """Checks that need no tools: safety and import resolution. `repo_root` None skips resolution."""
    has_tests = bool(re.search(r"\btest\s*\(", code))
    checks = [check("spec has tests", has_tests, "" if has_tests else "no `test(` block found")]
    specifiers = imports(code)
    unsafe = [s for s in specifiers if s.removeprefix("node:") in _NODE_BUILTINS or s.startswith("node:")]
    unsafe += [label for pattern, label in _DANGEROUS if pattern.search(code)]
    checks.append(check("no Node APIs or dynamic code", not unsafe,
                        f"not allowed in a generated spec: {', '.join(unsafe)}" if unsafe else ""))
    if repo_root is None:
        return checks
    root = repo_root.resolve()
    spec_dir = (root / spec_rel).parent
    known = package_names(repo_root)
    missing = []
    for spec in specifiers:
        if spec.startswith("."):
            base = (spec_dir / spec).resolve()
            inside = base == root or root in base.parents
            if not inside or not any(Path(f"{base}{suffix}").is_file() for suffix in _RESOLVE_SUFFIXES):
                missing.append(spec)
        elif not spec.startswith("node:") and spec not in _NODE_BUILTINS and _package_name(spec) not in known:
            missing.append(spec)
    checks.append(check("imports resolve", not missing,
                        f"modules that do not exist in the test repository: {', '.join(missing)}" if missing
                        else f"{len(specifiers)} import(s) resolved"))
    return checks


def _diagnostics_for(result: CommandResult, rel_paths: List[str]) -> List[str]:
    """Compiler/linter lines about our files only; pre-existing problems elsewhere are not ours."""
    variants = {p for rel in rel_paths for p in (rel, rel.replace("/", "\\"))}
    text = f"{result.stdout}\n{result.stderr}"
    lines = text.splitlines()
    ours: List[str] = []
    for i, line in enumerate(lines):
        if any(v in line for v in variants):
            ours.append(line)
            # ESLint prints the file once, then indented "line:col error ..." rows.
            for follow in lines[i + 1:]:
                if not follow.startswith(" ") or not follow.strip():
                    break
                ours.append(follow)
    return ours


def repo_checks(spec_path: Path, target_url: str, logs: List[str]) -> List[Check]:
    """Load, type-check and lint the spec in the test repository (the spec is on disk)."""
    assert TEST_REPO.path is not None
    rel = spec_path.resolve().relative_to(TEST_REPO.path.resolve()).as_posix()
    checks: List[Check] = []

    listing = TEST_REPO.list_tests(spec_path, target_url)
    logs.append(listing.as_log())
    checks.append(check("playwright --list (spec loads)", listing.ok,
                        "" if listing.ok else truncate_middle(f"{listing.stdout}\n{listing.stderr}".strip(), 4000),
                        retryable=not listing.timed_out))

    typed = TEST_REPO.typecheck(spec_path, target_url)
    if typed is None:
        checks.append(check("tsc --noEmit", True, "TypeScript compiler not installed in the test repo - skipped "
                            "(playwright --list still transpiles the spec)", skipped=True))
    else:
        logs.append(typed.as_log())
        ours = _diagnostics_for(typed, [rel])
        if typed.timed_out:
            checks.append(check("tsc --noEmit", False, "type check timed out", retryable=False))
        else:
            note = "" if typed.ok or ours else "errors only in other files (pre-existing) - not counted"
            checks.append(check("tsc --noEmit", not ours, "\n".join(ours[:40]) or note))

    linted = TEST_REPO.lint([spec_path], target_url)
    if linted is None:
        checks.append(check("eslint", True, "no ESLint set up in the test repo - skipped", skipped=True))
    else:
        logs.append(linted.as_log())
        if linted.returncode == 2 or linted.timed_out:
            checks.append(check("eslint", False, "ESLint could not run (exit 2 / timeout)", retryable=False))
        else:
            ours = _diagnostics_for(linted, [rel])
            checks.append(check("eslint", linted.ok, "\n".join(ours[:40]) or truncate_middle(linted.stdout, 2000)))
    return checks


def feedback(checks: List[Check]) -> str:
    failed = [c for c in checks if not c["ok"] and not c["skipped"]]
    return truncate_middle("\n\n".join(f"[{c['name']}]\n{c['detail']}" for c in failed), 6000)


@agent_node("Static Validation", engine="local-cli", abort_on_error=False, span_type="guardrail")
def validate_spec(state: QAState) -> Dict[str, Any]:
    if ROUTER.resolve(state.get("llm_provider")) == "offline":
        return {"validation": {"ok": True, "skipped": "offline provider - nothing is written or validated",
                               "checks": []}}
    ticket_id = state["ticket_id"]
    code = state.get("generated_typescript_code") or ""
    attempt = int(state.get("spec_attempts") or 0)
    spec_rel = f"{SETTINGS.test_spec_dir.strip('/')}/{ticket_id}.spec.ts"
    logs: List[str] = []
    checks: List[Check] = []

    unavailable = repo_available(state)
    if not automatable_cases(state):
        checks.append(check("spec", True, "no automatable test cases - only the Xray test cases are published",
                            skipped=True))
    elif not code:
        checks.append(check("spec produced", False, state.get("spec_feedback") or "the model returned no spec"))
    else:
        root = None if unavailable else (TEST_REPO.path.resolve() if TEST_REPO.path else None)
        checks += code_checks(code, spec_rel, root)
    if unavailable:
        checks.append(check("test repository", False, unavailable, retryable=False))

    if code and not unavailable and all(c["ok"] for c in checks):
        with REPO_LOCK:
            try:
                with TEST_REPO.ticket_files_on_base(ticket_id, code, xray.ticket_dir(ticket_id), {}, logs) as (spec, _):
                    assert spec is not None
                    checks += repo_checks(spec, state.get("target_url", ""), logs)
            except CommandError as exc:
                if exc.result is not None:
                    logs.append(exc.result.as_log())
                checks.append(check("test repository", False, str(exc), retryable=False))

    ok = all(c["ok"] or c["skipped"] for c in checks)
    retryable = not ok and all(c["retryable"] for c in checks if not c["ok"] and not c["skipped"])
    exhausted = attempt >= max_generations()
    validation = {"ok": ok, "retryable": retryable, "attempt": attempt, "checks": checks,
                  "spec_path": spec_rel if code else ""}
    update: Dict[str, Any] = {"validation": validation, "execution_logs": append_log(state, logs),
                              "spec_feedback": "" if ok else feedback(checks)}
    if TEST_REPO.path is not None and code:
        update["spec_file_path"] = str(TEST_REPO.path / spec_rel)
    if not ok and (not retryable or exhausted):
        failed = ", ".join(c["name"] for c in checks if not c["ok"] and not c["skipped"])
        update["errors"] = [f"Static validation failed after {attempt} attempt(s) ({failed}) - "
                            "nothing was run or published."]
        update["execution_passed"] = False
    return update


@agent_node("Playwright Runner", engine="local-cli", abort_on_error=False, span_type="tool")
def run_tests(state: QAState) -> Dict[str, Any]:
    """Run the validated spec against the target URL. A failing test is a finding, not a gate."""
    if ROUTER.resolve(state.get("llm_provider")) == "offline":
        return {}
    code = state.get("generated_typescript_code") or ""
    if not code or not (state.get("validation") or {}).get("ok"):
        return {"execution_passed": False}
    ticket_id = state["ticket_id"]
    logs: List[str] = []
    passed = False
    with REPO_LOCK:
        try:
            with TEST_REPO.ticket_files_on_base(ticket_id, code, xray.ticket_dir(ticket_id), {}, logs) as (spec, _):
                assert spec is not None
                passed = TEST_REPO.run_playwright(spec, state.get("target_url", ""), logs)
        except CommandError as exc:
            if exc.result is not None:
                logs.append(exc.result.as_log())
            logs.append(f"[error] {exc}")
            return {"execution_passed": False, "execution_logs": append_log(state, logs),
                    "errors": [f"Playwright Runner: {exc}"]}
    log_text = append_log(state, logs)
    return {"execution_passed": passed, "execution_logs": log_text,
            "playwright": {"passed": passed, "counts": parse_playwright_counts("\n".join(logs))}}

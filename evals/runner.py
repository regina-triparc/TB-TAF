"""
Eval runner: for every case in evals/cases, run the agents through the LLM router
and grade their output with evals/graders.py (and, optionally, an LLM judge).

    python -m taf eval                              # all cases, current default provider
    python -m taf eval --provider ollama            # or claude / auto / offline
    python -m taf eval --case tb-601 --stage architect
    python -m taf eval --with-repo                  # + playwright --list / tsc / eslint in the test repo
    python -m taf eval --with-repo --execute        # + run the generated tests (needs the target app)
    python -m taf eval --judge                      # + LLM judge (judges/scenario_quality.v1.md)
    python -m taf eval --langfuse --label prompt-v2 # + traces and scores in Langfuse (environment "eval")
    python -m taf eval compare A.json B.json        # what changed between two result files

Evals are separate from production runs: no run history, no approval, no commit,
no push, no PR. With --with-repo the spec is written onto the test repo's base
branch only for the checks and removed again. Langfuse is off unless --langfuse,
and then traces go to the "eval" environment.

Results are saved to evals/results/<timestamp>-<provider>.json (git-ignored) with
the metadata needed to compare them: git commit, prompt hashes, models, dataset
version, judge prompt version.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from evals import graders

CASES_DIR = Path(__file__).parent / "cases"
RESULTS_DIR = Path(__file__).parent / "results"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAGES = ["triage", "architect", "automation"]


def load_cases(only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]
    if only:
        cases = [c for c in cases if c["id"] in only]
    return cases


# --------------------------------------------------------------- metadata --
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def dataset_version() -> str:
    """Hash of every case file: two results with the same version graded the same dataset."""
    digest = hashlib.sha256()
    for path in sorted(CASES_DIR.glob("*.json")):
        digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:12]


def prompt_versions() -> Dict[str, str]:
    """sha256 of every agent / step prompt and skill, keyed by path."""
    files = sorted((PROJECT_ROOT / "taf").rglob("*.md")) + sorted((PROJECT_ROOT / "skills").rglob("*.md"))
    return {p.relative_to(PROJECT_ROOT).as_posix(): _sha(p.read_bytes()) for p in files if "README" not in p.name}


def git_state() -> Dict[str, Any]:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True,
                                  timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
            "dirty": bool(git("status", "--porcelain", "--", "taf", "evals", "skills"))}


def metadata(args: argparse.Namespace, resolved: str) -> Dict[str, Any]:
    from taf.llm.router import ROUTER

    routing = ROUTER.describe(args.provider or None)
    meta: Dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": args.label or "",
        "provider": resolved,
        "stage": args.stage,
        "models": {k: f"{v['provider']}:{v['model']}" + (f" ({v['effort']})" if v["effort"] != "n/a" else "")
                   for k, v in routing["routes"].items()},
        "dataset_version": dataset_version(),
        "prompts": prompt_versions(),
        "git": git_state(),
        "options": {"with_repo": args.with_repo, "execute": args.execute, "judge": args.judge or "",
                    "langfuse": args.langfuse},
    }
    if args.judge:
        from evals.judge import judge_prompt

        prompt = judge_prompt(args.judge)
        judge_route = ROUTER.route("review", args.judge_provider or None)
        meta["judge"] = {"prompt": prompt["name"], "prompt_sha256": prompt["sha256"],
                         "model": f"{judge_route.provider}:{judge_route.model}"}
    return meta


# ----------------------------------------------------------------- running --
def _initial_state(case: Dict[str, Any], provider: str) -> Dict[str, Any]:
    from taf.integrations.jira.board import get_board

    ticket = case["ticket"]
    summary, description = ticket.get("summary", ""), ticket.get("description", "")
    if not summary and ticket.get("from_seed"):
        issue = get_board().issue(ticket["key"]) or {}
        summary, description = issue.get("summary", ""), issue.get("description", "")
    if not summary:
        raise ValueError(f"case {case['id']}: no summary (inline or from the seed file)")
    state: Dict[str, Any] = {
        "run_id": f"eval-{case['id']}-{uuid.uuid4().hex[:8]}",
        "ticket_id": ticket["key"],
        "summary": summary,
        "description": description,
        "target_url": ticket.get("target_url", "https://app.example.test"),
        "github_diff": ticket.get("github_diff", ""),
        "environment_logs": "",
        "errors": [],
    }
    if provider:
        state["llm_provider"] = provider
    return state


def _repo_checks(state: Dict[str, Any], code: str, execute: bool) -> Dict[str, Any]:
    """Compile / lint / load (and optionally run) the spec in the test repo, then remove it."""
    from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, CommandError
    from taf.integrations.jira import xray
    from taf.steps.validation import code_checks, repo_checks

    logs: List[str] = []
    out: Dict[str, Any] = {"checks": [], "passed": None}
    with REPO_LOCK:
        try:
            TEST_REPO.ensure_ready(logs)
            if not TEST_REPO.install_dependencies(state["target_url"], logs):
                out["checks"].append({"name": "test repository", "ok": False, "detail": "dependencies not installed"})
                return out
            assert TEST_REPO.path is not None
            spec_rel = f"{TEST_REPO.settings.test_spec_dir.strip('/')}/{state['ticket_id']}.spec.ts"
            static = code_checks(code, spec_rel, TEST_REPO.path.resolve())
            out["checks"] += static
            if not all(c["ok"] for c in static):
                return out  # unsafe or unresolvable code is never loaded
            with TEST_REPO.ticket_files_on_base(state["ticket_id"], code, xray.ticket_dir(state["ticket_id"]), {},
                                                logs) as (spec, _):
                assert spec is not None
                out["checks"] += repo_checks(spec, state["target_url"], logs)
                if execute and all(c["ok"] or c["skipped"] for c in out["checks"]):
                    out["passed"] = TEST_REPO.run_playwright(spec, state["target_url"], logs)
        except CommandError as exc:
            out["checks"].append({"name": "test repository", "ok": False, "detail": str(exc)})
    return out


def _repo_context(state: Dict[str, Any]) -> None:
    """The same test-repo conventions the production Automation Engineer is shown."""
    from taf.config import SETTINGS
    from taf.integrations.github.repository import TEST_REPO
    from taf.steps.context import render_repo_context, scan_repository

    if TEST_REPO.path and TEST_REPO.path.is_dir():
        facts = scan_repository(TEST_REPO.path.resolve(), SETTINGS.test_spec_dir)
        facts["prompt"] = render_repo_context(facts, SETTINGS.repo_context_max_chars)
        state["repo_context"] = facts


def run_case(case: Dict[str, Any], provider: str, last_stage: str,
             options: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    from taf.agents import automation
    from taf.agents.architect import test_architect
    from taf.agents.triage import intake_triager
    from taf.llm.budget import BUDGET
    from taf.llm.router import ROUTER
    from taf.observability.tracing import TRACER

    opts = options or argparse.Namespace(with_repo=False, execute=False, judge="", judge_provider="", langfuse=False)
    state = _initial_state(case, provider)
    expect = case.get("expect", {})
    result: Dict[str, Any] = {"id": case["id"], "ticket": state["ticket_id"], "run_id": state["run_id"], "stages": {}}
    stages = STAGES[: STAGES.index(last_stage) + 1]
    with TRACER.run(state, extra_tags=["eval", f"case:{case['id']}"]) as root:
        try:
            for stage in stages:
                started = time.monotonic()
                if stage == "triage":
                    state.update(intake_triager(state))
                    checks = graders.grade_triage(state.get("triage_analysis") or {}, expect.get("triage", {}))
                elif stage == "architect":
                    state.update(test_architect(state))
                    criteria = (state.get("triage_analysis") or {}).get("acceptance_criteria") or []
                    checks = graders.grade_plan(state.get("test_cases") or [], expect.get("architect", {}), criteria)
                else:
                    if opts.with_repo:
                        _repo_context(state)
                    route = ROUTER.route("automation", provider or None)
                    with TRACER.node(state, "generate_spec", as_type="agent"):
                        answer = ROUTER.text(state, route, automation.build_messages(state, route))
                    code, problem = automation.parse_spec(answer)
                    state["generated_typescript_code"] = code or ""
                    checks = graders.grade_spec(code or "", expect.get("automation", {}), state.get("test_cases"))
                    if problem:
                        checks.insert(0, graders.check("spec parses", False, problem, dimension="typescript"))
                    if opts.with_repo and code:
                        repo = _repo_checks(state, code, opts.execute)
                        seen = {c["name"] for c in checks}  # the static checks are already graded above
                        checks += graders.grade_repo_checks([r for r in repo["checks"] if r["name"] not in seen])
                        if repo["passed"] is not None:
                            checks.append(graders.check("generated tests pass", repo["passed"],
                                                        "a pass is not proof of coverage", hard=False,
                                                        dimension="execution"))
                if state.get("errors"):
                    checks.insert(0, graders.check("agent ran without error", False,
                                                   "; ".join(state["errors"])[:400]))
                result["stages"][stage] = {"seconds": round(time.monotonic() - started, 1),
                                           "checks": checks, **graders.score(checks)}
                if state.get("aborted") or state.get("errors"):
                    break
            if opts.judge and result["stages"]:
                from evals.judge import judge

                try:
                    result["judge"] = judge(state, opts.judge_provider or provider, opts.judge)
                except Exception as exc:  # noqa: BLE001 - a judge failure is reported, not fatal
                    result["judge"] = {"error": f"{type(exc).__name__}: {exc}"[:400], "prompt": opts.judge}
        finally:
            result["cloud_cost_usd"] = round(BUDGET.run_cost_usd(state["run_id"]), 4)
            BUDGET.release(state["run_id"])
        result["passed"] = bool(result["stages"]) and all(s["passed"] for s in result["stages"].values()) \
            and len(result["stages"]) == len(stages)
        _score_in_langfuse(result)
        root.update(output={"passed": result["passed"], "stages": {k: {"hard": v["hard"], "soft": v["soft"]}
                                                                   for k, v in result["stages"].items()}})
    return result


def _score_in_langfuse(result: Dict[str, Any]) -> None:
    from taf.observability.tracing import TRACER

    run_id = result["run_id"]
    TRACER.score(run_id, "eval.passed", 1 if result["passed"] else 0, "BOOLEAN", environment="eval")
    for stage, data in result["stages"].items():
        hard_ok, hard_n = (int(x) for x in data["hard"].split("/"))
        soft_ok, soft_n = (int(x) for x in data["soft"].split("/"))
        TRACER.score(run_id, f"eval.{stage}.hard", hard_ok / hard_n if hard_n else 1.0, environment="eval")
        if soft_n:
            TRACER.score(run_id, f"eval.{stage}.soft", soft_ok / soft_n, environment="eval")
    judged = result.get("judge") or {}
    for dim, value in (judged.get("scores") or {}).items():
        TRACER.score(run_id, f"judge.{dim}", value, comment=f"{judged.get('prompt')} by {judged.get('model')}",
                     environment="eval")


def _print(result: Dict[str, Any]) -> None:
    print(f"\n{result['id']} ({result['ticket']}) - {'PASS' if result['passed'] else 'FAIL'}"
          f"  cost ${result['cloud_cost_usd']:.4f}")
    for stage, data in result["stages"].items():
        print(f"  {stage:<11} hard {data['hard']:<6} soft {data['soft']:<6} {data['seconds']}s")
        for c in data["checks"]:
            if not c["passed"]:
                print(f"     {'x' if c['hard'] else '~'} {c['name']}: {c['detail']}")
    judged = result.get("judge") or {}
    if judged.get("scores"):
        print(f"  judge       mean {judged['mean']}  " + ", ".join(f"{k} {v}" for k, v in judged["scores"].items()))
    elif judged.get("error") or judged.get("skipped"):
        print(f"  judge       {judged.get('error') or judged.get('skipped')}")


def _isolate_tracing(enabled: bool) -> None:
    """Evals never trace into the production environment: off, or the 'eval' environment."""
    from taf.observability import tracing

    tracer = tracing.TRACER
    if not tracer._init_done:  # settings are read when the client starts
        tracer.settings = dataclasses.replace(tracer.settings, langfuse_enabled=enabled,
                                              langfuse_environment="eval")


# ----------------------------------------------------------------- compare --
def _dimension_totals(results: List[Dict[str, Any]]) -> Dict[str, List[int]]:
    totals: Dict[str, List[int]] = {}
    for result in results:
        for stage in result["stages"].values():
            for c in stage["checks"]:
                dim = c.get("dimension") or "other"
                got = totals.setdefault(dim, [0, 0])
                got[0] += int(c["passed"])
                got[1] += 1
    return totals


def compare(path_a: str, path_b: str) -> int:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (path_a, path_b))
    ma, mb = a.get("metadata", {}), b.get("metadata", {})
    print(f"A: {path_a}\nB: {path_b}\n")
    for key in ("label", "provider", "stage", "dataset_version"):
        if ma.get(key) != mb.get(key):
            print(f"  {key}: {ma.get(key)!r} -> {mb.get(key)!r}")
    for agent in sorted(set(ma.get("models", {})) | set(mb.get("models", {}))):
        if ma.get("models", {}).get(agent) != mb.get("models", {}).get(agent):
            print(f"  model {agent}: {ma.get('models', {}).get(agent)} -> {mb.get('models', {}).get(agent)}")
    changed = [p for p in sorted(set(ma.get("prompts", {})) | set(mb.get("prompts", {})))
               if ma.get("prompts", {}).get(p) != mb.get("prompts", {}).get(p)]
    if changed:
        print("  prompts changed: " + ", ".join(changed))
    ga, gb = ma.get("git", {}), mb.get("git", {})
    if ga.get("commit") != gb.get("commit"):
        print(f"  code: {ga.get('commit', '')[:10]} -> {gb.get('commit', '')[:10]}"
              + (" (B has uncommitted changes)" if gb.get("dirty") else ""))
    if ma.get("dataset_version") != mb.get("dataset_version"):
        print("  ! different datasets - case-level comparison only")

    print("\nper case / stage (hard, soft):")
    by_id = {r["id"]: r for r in a["results"]}
    for rb in b["results"]:
        ra = by_id.get(rb["id"])
        for stage, sb in rb["stages"].items():
            sa = (ra or {}).get("stages", {}).get(stage)
            before = f"{sa['hard']:>5} {sa['soft']:>5}" if sa else "    -     -"
            mark = "" if not sa or sa["passed"] == sb["passed"] else ("  FIXED" if sb["passed"] else "  BROKE")
            print(f"  {rb['id']:<26} {stage:<11} {before}  ->  {sb['hard']:>5} {sb['soft']:>5}{mark}")
        ja, jb = ((ra or {}).get("judge") or {}).get("mean"), (rb.get("judge") or {}).get("mean")
        if ja is not None or jb is not None:
            print(f"  {rb['id']:<26} {'judge':<11} {ja if ja is not None else '-':>11}  ->  {jb if jb is not None else '-':>11}")

    print("\nper dimension (checks passed):")
    ta, tb = _dimension_totals(a["results"]), _dimension_totals(b["results"])
    for dim in sorted(set(ta) | set(tb)):
        pa, pb = ta.get(dim, [0, 0]), tb.get(dim, [0, 0])
        print(f"  {dim:<14} {pa[0]:>3}/{pa[1]:<3} -> {pb[0]:>3}/{pb[1]:<3}")
    print(f"\ncases passed: {sum(r['passed'] for r in a['results'])}/{len(a['results'])} -> "
          f"{sum(r['passed'] for r in b['results'])}/{len(b['results'])}")
    return 0


# -------------------------------------------------------------------- main --
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m taf eval")
    parser.add_argument("--provider", default="")
    parser.add_argument("--case", action="append")
    parser.add_argument("--stage", default="automation", choices=STAGES)
    parser.add_argument("--with-repo", action="store_true",
                        help="type-check, lint and load the spec in the test repo (written temporarily, never committed)")
    parser.add_argument("--execute", action="store_true", help="with --with-repo: also run the generated tests")
    parser.add_argument("--judge", nargs="?", const="scenario_quality.v1", default="",
                        help="LLM judge prompt version (default scenario_quality.v1)")
    parser.add_argument("--judge-provider", default="", help="provider for the judge (default: --provider)")
    parser.add_argument("--langfuse", action="store_true", help="trace and score in Langfuse, environment 'eval'")
    parser.add_argument("--label", default="", help="free text saved with the results, e.g. 'prompt-v2'")
    parser.add_argument("--out", default="", help="results file (default evals/results/<time>-<provider>.json)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or [])
    if argv[:1] == ["compare"]:
        if len(argv) != 3:
            print("usage: python -m taf eval compare A.json B.json")
            return 2
        return compare(argv[1], argv[2])
    args = build_parser().parse_args(argv)
    _isolate_tracing(args.langfuse)

    from taf.llm.router import ROUTER
    from taf.observability.tracing import TRACER

    cases = load_cases(args.case)
    if not cases:
        print("No eval cases matched.")
        return 2
    resolved = ROUTER.resolve(args.provider or None)
    print(f"Evaluating {len(cases)} case(s) on provider {resolved}, through stage {args.stage}"
          + (" + test repo checks" if args.with_repo else "") + (f" + judge {args.judge}" if args.judge else ""))
    meta = metadata(args, resolved)
    try:
        results = [run_case(case, args.provider, args.stage, args) for case in cases]
    finally:
        TRACER.flush()
    for result in results:
        _print(result)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{resolved}.json"
    out.write_text(json.dumps({"metadata": meta, "provider": resolved, "stage": args.stage, "results": results},
                              indent=2), encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    shown = out.relative_to(Path.cwd()) if out.resolve().is_relative_to(Path.cwd()) else out
    print(f"\n{passed}/{len(results)} case(s) passed. Saved {shown}")
    print(f"Compare with an earlier run: python -m taf eval compare <earlier.json> {shown}")
    return 0 if passed == len(results) else 1

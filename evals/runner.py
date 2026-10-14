"""
Eval runner: for every case in evals/cases, run the agents through the LLM router
and grade their output with evals/graders.py.

    python -m taf eval                          # all cases, current default provider
    python -m taf eval --provider ollama        # or claude / auto / offline
    python -m taf eval --case tb-601 --stage architect

Nothing is written to the test repository and Playwright is not run: the
Automation Engineer's spec is generated and graded as text. Results are printed
and saved to evals/results/<timestamp>-<provider>.json (git-ignored), so two
providers or two prompt versions can be compared.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from evals import graders

CASES_DIR = Path(__file__).parent / "cases"
RESULTS_DIR = Path(__file__).parent / "results"
STAGES = ["triage", "architect", "automation"]


def load_cases(only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]
    if only:
        cases = [c for c in cases if c["id"] in only]
    return cases


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
        "run_id": f"eval-{uuid.uuid4().hex[:12]}",
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


def run_case(case: Dict[str, Any], provider: str, last_stage: str) -> Dict[str, Any]:
    from taf.agents import automation
    from taf.agents.architect import test_architect
    from taf.agents.triage import intake_triager
    from taf.llm.budget import BUDGET
    from taf.llm.router import ROUTER

    state = _initial_state(case, provider)
    expect = case.get("expect", {})
    result: Dict[str, Any] = {"id": case["id"], "ticket": state["ticket_id"], "stages": {}}
    stages = STAGES[: STAGES.index(last_stage) + 1]
    try:
        for stage in stages:
            started = time.monotonic()
            if stage == "triage":
                state.update(intake_triager(state))
                checks = graders.grade_triage(state.get("triage_analysis") or {}, expect.get("triage", {}))
            elif stage == "architect":
                state.update(test_architect(state))
                checks = graders.grade_plan(state.get("test_cases") or [], expect.get("architect", {}))
            else:
                route = ROUTER.route("automation", provider or None)
                answer = ROUTER.text(state, route, automation.build_messages(state, route))
                code, problem = automation.parse_spec(answer)
                state["generated_typescript_code"] = code or ""
                checks = graders.grade_spec(code or "", expect.get("automation", {}))
                if problem:
                    checks.insert(0, graders.check("spec parses", False, problem))
            if state.get("errors"):
                checks.insert(0, graders.check("agent ran without error", False, "; ".join(state["errors"])[:400]))
            result["stages"][stage] = {"seconds": round(time.monotonic() - started, 1),
                                       "checks": checks, **graders.score(checks)}
            if state.get("aborted") or state.get("errors"):
                break
    finally:
        result["cloud_cost_usd"] = round(BUDGET.run_cost_usd(state["run_id"]), 4)
        BUDGET.release(state["run_id"])
    result["passed"] = bool(result["stages"]) and all(s["passed"] for s in result["stages"].values()) \
        and len(result["stages"]) == len(stages)
    return result


def _print(result: Dict[str, Any]) -> None:
    print(f"\n{result['id']} ({result['ticket']}) - {'PASS' if result['passed'] else 'FAIL'}"
          f"  cost ${result['cloud_cost_usd']:.4f}")
    for stage, data in result["stages"].items():
        print(f"  {stage:<11} hard {data['hard']:<6} soft {data['soft']:<6} {data['seconds']}s")
        for c in data["checks"]:
            if not c["passed"]:
                print(f"     {'x' if c['hard'] else '~'} {c['name']}: {c['detail']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taf eval")
    parser.add_argument("--provider", default="")
    parser.add_argument("--case", action="append")
    parser.add_argument("--stage", default="automation", choices=STAGES)
    args = parser.parse_args(argv)

    from taf.llm.router import ROUTER

    cases = load_cases(args.case)
    if not cases:
        print("No eval cases matched.")
        return 2
    resolved = ROUTER.resolve(args.provider or None)
    print(f"Evaluating {len(cases)} case(s) on provider {resolved}, through stage {args.stage}")
    results = [run_case(case, args.provider, args.stage) for case in cases]
    for result in results:
        _print(result)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{resolved}.json"
    out.write_text(json.dumps({"provider": resolved, "stage": args.stage, "results": results}, indent=2),
                   encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} case(s) passed. Saved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
    return 0 if passed == len(results) else 1

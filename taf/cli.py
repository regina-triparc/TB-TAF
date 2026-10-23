"""
Terminal interface: `python -m taf <command>`.

    doctor                   check this machine's setup and say what is missing
    providers [--set P]      show the LLM routing table; --set switches it (with --server, on a running service)
    board                    list the queue (Jira, or the seed file when Jira is not configured)
    skills                   list skills and the agents they apply to
    run TICKET [options]     run pipeline stages for one ticket, in this process, and print what each agent did
    eval [options]           score agent output against evals/cases (see evals/README.md)
    xray export TICKET       import reviewed Xray test cases from the test repo (dry run unless --apply)
    serve [--reload]         start the API + dashboard (same as `uvicorn main:app`)

Runs started here are recorded in the same run store as the dashboard, so they
appear on the board too.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

_MARK = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]", "info": "[info]"}


def _out(kind: str, text: str) -> None:
    print(f"{_MARK[kind]} {text}")


# ------------------------------------------------------------------ doctor --
def cmd_doctor(_args: argparse.Namespace) -> int:
    import requests

    from taf.config import PROJECT_ROOT, SETTINGS, project_path
    from taf.integrations.jira.board import get_board
    from taf.llm.router import ROUTER
    from taf.skills import load_skills

    failures = 0

    def check(ok: bool, good: str, bad: str, fatal: bool = True) -> None:
        nonlocal failures
        if ok:
            _out("ok", good)
        else:
            _out("fail" if fatal else "warn", bad)
            failures += int(fatal)

    print(f"Project: {PROJECT_ROOT}\n")
    check(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}",
          f"Python {sys.version.split()[0]} - 3.11+ is required")
    check((PROJECT_ROOT / ".env").is_file(), ".env present",
          ".env missing - copy .env.example to .env (defaults work for an offline run)", fatal=False)

    resolved = ROUTER.resolve()
    _out("info", f"LLM provider: {ROUTER.default_provider} -> {resolved}")
    needs_ollama = resolved in ("ollama", "claude")
    if resolved == "claude":
        check(bool(SETTINGS.anthropic_api_key), "ANTHROPIC_API_KEY set",
              "ANTHROPIC_API_KEY empty - set it, or use LLM_PROVIDER=ollama / offline")
    if needs_ollama:
        try:
            requests.get(SETTINGS.ollama_base_url.rstrip("/") + "/models", timeout=3).raise_for_status()
            _out("ok", f"Ollama reachable at {SETTINGS.ollama_base_url}")
        except requests.RequestException:
            fatal = resolved == "ollama"
            check(False, "", f"Ollama not reachable at {SETTINGS.ollama_base_url} - "
                  + ("start `ollama serve`" if fatal else "the report step will use its fallback"), fatal=fatal)
    if resolved == "offline":
        _out("info", "offline provider: placeholder output only, no model needed")

    jira = get_board().tickets()
    keys = ", ".join(t.get("key", "?") for t in jira["tickets"][:5])
    check(bool(jira["tickets"]), f"Queue: {len(jira['tickets'])} ticket(s) ({'Jira' if jira['configured'] else 'seed file'}): {keys}",
          f"Queue empty - {jira['reason'] or 'no tickets'}", fatal=False)

    if SETTINGS.test_repo_local_path:
        repo = project_path(SETTINGS.test_repo_local_path).resolve()
        managed = not SETTINGS.no_git
        if managed:
            _out("info", f"Test repo: managed clone of {SETTINGS.test_repo_url or '(TEST_REPO_URL not set)'} "
                         f"at {repo}; pushes qa/<TICKET> branches: {'yes' if SETTINGS.git_push_enabled else 'no'}")
            if SETTINGS.test_repo_url and shutil.which("git"):
                import subprocess
                env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
                probe = subprocess.run(["git", "ls-remote", "--heads", SETTINGS.test_repo_url],
                                       capture_output=True, text=True, timeout=30, env=env)
                check(probe.returncode == 0, "test repo reachable with your git credentials",
                      "cannot read TEST_REPO_URL - ask for access to the repo, sign in to GitHub "
                      "with git once, or set GITHUB_TOKEN", fatal=False)
            if not repo.is_dir():
                _out("info", "agent clone not created yet - the first automation run clones it and runs npm ci")
        else:
            check(repo.is_dir(), f"TEST_REPO_LOCAL_PATH exists: {repo}",
                  f"TEST_REPO_LOCAL_PATH does not exist: {repo}", fatal=False)
        if repo.is_dir():
            check((repo / SETTINGS.test_spec_dir).is_dir(), f"TEST_SPEC_DIR exists: {SETTINGS.test_spec_dir}",
                  f"TEST_SPEC_DIR missing in the test repo: {SETTINGS.test_spec_dir}", fatal=False)
            check((repo / "node_modules").is_dir(), "test repo has node_modules",
                  "test repo has no node_modules - the next automation run installs them", fatal=False)
        env_file = project_path(SETTINGS.test_repo_env_file) if SETTINGS.test_repo_env_file else None
        check(bool(env_file and env_file.is_file()) or (repo / ".env").is_file(),
              f"test login available ({'TEST_REPO_ENV_FILE' if env_file and env_file.is_file() else 'test repo .env'})",
              "no test login: set TEST_REPO_ENV_FILE to a .env with TBA_E2E_EMAIL / TBA_E2E_PASSWORD "
              "(or put one in the test repo) - generated specs cannot sign in without it", fatal=False)
    else:
        _out("warn", "TEST_REPO_LOCAL_PATH not set - triage/architect stages work, automation will not")
    for tool in ("node", "npx", "git"):
        check(bool(shutil.which(tool)), f"{tool} on PATH", f"{tool} not on PATH (needed for the automation stage)",
              fatal=False)

    _out("info", f"Workflow: approval {'required' if SETTINGS.require_approval else 'not required'}; "
                 f"push {'on' if SETTINGS.git_push_enabled else 'off'}; pull requests "
                 f"{'on' if SETTINGS.pr_create_enabled else 'off'}; checkpoints in {SETTINGS.qa_checkpoint_db_path}")
    if SETTINGS.pr_create_enabled:
        check(bool(SETTINGS.github_token or shutil.which("gh")), "pull requests: GITHUB_TOKEN or gh available",
              "PR_CREATE_ENABLED=true needs GITHUB_TOKEN or a signed-in GitHub CLI (gh)", fatal=False)
    from taf.observability.tracing import TRACER
    if TRACER.enabled:
        ok, why = TRACER.check()
        check(ok, f"Langfuse tracing on ({SETTINGS.langfuse_base_url or 'cloud default'}): {why}",
              f"Langfuse configured but {why} - runs continue untraced", fatal=False)
    else:
        _out("info", f"Langfuse tracing {TRACER.status}")

    if SETTINGS.page_contract_path:
        check(project_path(SETTINGS.page_contract_path).is_file(), f"page contract: {SETTINGS.page_contract_path}",
              f"PAGE_CONTRACT_PATH unreadable: {SETTINGS.page_contract_path}", fatal=False)
    skills = load_skills()
    _out("info", f"{len(skills)} skill(s) in {SETTINGS.skills_dir}/ "
                 f"({'enabled' if SETTINGS.skills_enabled else 'disabled'}; on local models: "
                 f"{'yes' if SETTINGS.skills_on_local else 'no'})")

    print()
    if failures:
        print(f"{failures} blocking problem(s). Fix those, or try: python -m taf run TB-601 --provider offline")
        return 1
    print("Ready. Try: python -m taf run TB-601 --stage architect")
    return 0


# --------------------------------------------------------------- providers --
def cmd_providers(args: argparse.Namespace) -> int:
    if args.server:
        import requests
        base = args.server.rstrip("/")
        try:
            if args.set:
                resp = requests.post(f"{base}/dashboard/api/provider", json={"provider": args.set}, timeout=10)
            else:
                resp = requests.get(f"{base}/dashboard/api/provider", timeout=10)
        except requests.RequestException as exc:
            _out("fail", f"Cannot reach {base}: {exc}")
            return 1
        if not resp.ok:
            _out("fail", f"{resp.status_code}: {resp.text[:300]}")
            return 1
        _print_routing(resp.json())
        return 0

    from taf.llm.router import ROUTER
    if args.set:
        ROUTER.set_default(args.set)
        _out("info", "Switched for this process only. To switch a running service add "
                     "--server http://127.0.0.1:8000; to make it permanent set LLM_PROVIDER in .env.")
    _print_routing(ROUTER.describe())
    return 0


def _print_routing(info: Dict[str, Any]) -> None:
    print(f"default provider: {info['default']}  (resolves to {info['resolved']})")
    print(f"available:        {', '.join(info['available'])}")
    print(f"claude key set:   {'yes' if info['claude_configured'] else 'no'}\n")
    print(f"  {'agent':<11} {'provider':<9} {'model':<28} effort")
    for key, r in info["routes"].items():
        print(f"  {key:<11} {r['provider']:<9} {r['model']:<28} {r['effort']}")
    report = info["report"]
    print(f"  {'report':<11} {report['provider']:<9} {report['model']:<28} n/a")


# ------------------------------------------------------------------- board --
def cmd_board(args: argparse.Namespace) -> int:
    from taf.integrations.jira.board import get_board
    jira = get_board().tickets(source=args.source)
    print(f"source: {'Jira' if jira['source'] == 'jira' else 'seed file'}"
          + (f"  ({jira['reason']})" if jira["reason"] else ""))
    for t in jira["tickets"]:
        print(f"  {t.get('key', ''):<10} {t.get('status', ''):<14} {t.get('summary', '')}")
    return 0


# ------------------------------------------------------------------ skills --
def cmd_skills(_args: argparse.Namespace) -> int:
    from taf.config import SETTINGS
    from taf.skills import load_skills
    skills = load_skills()
    print(f"{len(skills)} skill(s) in {SETTINGS.skills_dir}/ - enabled: {SETTINGS.skills_enabled}, "
          f"on local models: {SETTINGS.skills_on_local}")
    for s in skills:
        print(f"  {s.name:<30} agents={','.join(s.agents) or '-':<22} {s.description}")
    return 0


# --------------------------------------------------------------------- run --
def cmd_run(args: argparse.Namespace) -> int:
    from taf.llm.router import ROUTER
    from taf.runs import stages

    try:
        ROUTER.resolve(args.provider or None)
        state = stages.prepare(args.ticket, args.stage, summary=args.summary or "",
                               description=args.description or "", target_url=args.target_url or "",
                               provider=args.provider or "", source=args.source or "")
    except (stages.StageError, ValueError) as exc:
        _out("fail", str(exc))
        return 2

    routing = ROUTER.describe(args.provider or None)
    print(f"run {state['run_id']}  ticket {args.ticket} ({state['ticket_source']})  "
          f"stage {args.stage}  provider {routing['resolved']}")
    prs = ", ".join(f"#{p['number']} {p['status'].lower()}".strip() for p in state["pull_requests"])
    print(f"linked PRs: {prs or 'none found'}")
    print(f"target: {state['target_url'] or '(none - set DASHBOARD_TARGET_URL or --target-url)'}\n")

    final = stages.execute(state, args.stage, on_phase=_on_phase)
    return _finish_run(final, args)


def _on_phase(name: str, status: str, detail: str) -> None:
    if status == "running":
        print(f"  -> {name} ...", flush=True)
    else:
        _out("ok" if status in ("ok", "waiting") else "fail", f"{name}: {detail}")


def _finish_run(final: Dict[str, Any], args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        print(json.dumps({k: v for k, v in final.items() if not k.startswith("_")}, indent=2, default=str))
    else:
        _print_outputs(final, args.show)
    run_state = final.get("_run_state")
    print(f"\nrun state: {run_state}  (also on the dashboard: python -m taf serve)")
    if run_state == "waiting":
        request = final.get("_approval_request") or {}
        action = f"pushing {request.get('branch', '')}" if request.get("will_push") else "publishing"
        pr = " and opening a pull request" if request.get("will_open_pr") else ""
        print(f"\nWaiting for approval before {action}{pr}.")
        for finding in request.get("review_findings") or []:
            print(f"  review: {finding}")
        print(f"  approve:  python -m taf resume {final['run_id']} --approve --by <your name>")
        print(f"  reject:   python -m taf resume {final['run_id']} --reject --note <why>")
        return 0
    if run_state in ("cancelled", "failed") and final.get("run_id"):
        print(f"continue from the last completed step: python -m taf resume {final['run_id']}")
    return 0 if run_state == "completed" else 1


def cmd_resume(args: argparse.Namespace) -> int:
    from taf.runs import stages

    decision = None
    if args.approve or args.reject:
        who = args.by or os.getenv("USERNAME") or os.getenv("USER") or "cli"
        decision = {"approved": bool(args.approve), "by": who, "note": args.note or ""}
    try:
        final = stages.resume(args.run_id, decision, on_phase=_on_phase)
    except stages.StageError as exc:
        _out("fail", str(exc))
        return 2
    return _finish_run(final, args)


def _print_outputs(state: Dict[str, Any], show: List[str]) -> None:
    triage = state.get("triage_analysis") or {}
    if triage and ("triage" in show or "all" in show):
        print("\n== Triage ==")
        print(f"risk: {triage.get('risk_level')}  -  {triage.get('feature_summary')}")
        for c in triage.get("acceptance_criteria") or []:
            print(f"  - {c}")
    cases = state.get("test_cases") or []
    if cases and ("plan" in show or "all" in show):
        print(f"\n== Test plan ({len(cases)} cases) ==")
        for c in cases:
            print(f"  {c.get('test_id')} [{c.get('priority')}] {'auto' if c.get('automatable') else 'manual'}  {c.get('summary')}")
    if state.get("generated_typescript_code") and ("spec" in show or "all" in show):
        print("\n== Generated spec ==")
        print(state["generated_typescript_code"])
    if state.get("execution_logs") and ("logs" in show or "all" in show):
        print("\n== Execution log (tail) ==")
        print(state["execution_logs"][-3000:])
    if state.get("final_report") and ("report" in show or "all" in show):
        print("\n== Report ==")
        print(state["final_report"])
    if state.get("report_html_path"):
        print(f"\nHTML report (manual checklist): {state['report_html_path']}")


# -------------------------------------------------------------------- eval --
def cmd_eval(args: argparse.Namespace) -> int:
    from taf.config import PROJECT_ROOT
    if str(PROJECT_ROOT) not in sys.path:  # evals/ sits next to taf/, not inside it
        sys.path.insert(0, str(PROJECT_ROOT))
    from evals.runner import main as eval_main
    # Everything after `eval` goes to the eval runner (see `python -m taf eval --help`).
    return eval_main(args.eval_args)


# ------------------------------------------------------------------- serve --
def cmd_xray_export(args: argparse.Namespace) -> int:
    """
    Import a ticket's reviewed Xray test cases into Jira. Reads XRAY_DIR/<TICKET>/<TICKET>.xray.json
    from the test repo - by default from the base branch (what was merged after review) for the
    agent's own clone, or from the working tree when the test repo is used in place.
    """
    from taf.config import SETTINGS, project_path
    from taf.integrations.github.repository import run_command
    from taf.integrations.jira import xray, xray_cloud

    repo = project_path(args.test_repo or SETTINGS.test_repo_local_path).resolve() \
        if (args.test_repo or SETTINGS.test_repo_local_path) else None
    if repo is None or not repo.is_dir():
        _out("fail", f"test repo not found: {repo} (set TEST_REPO_LOCAL_PATH or pass --test-repo)")
        return 2
    rel = xray.json_file(args.ticket)
    ref = args.ref or ("" if SETTINGS.no_git else SETTINGS.test_repo_base_branch)
    env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    if ref:
        run_command(["git", "fetch", "--quiet", "origin", ref], cwd=repo, timeout_s=120, env=env)
        shown = run_command(["git", "show", f"origin/{ref}:{rel}"], cwd=repo, timeout_s=60, env=env)
        if not shown.ok:  # a local-only branch or a commit id
            shown = run_command(["git", "show", f"{ref}:{rel}"], cwd=repo, timeout_s=60, env=env)
        if not shown.ok:
            _out("fail", f"{rel} is not on {ref} in {repo}. Has the qa/{args.ticket} pull request been merged? "
                         f"(--ref qa/{args.ticket} exports the unmerged branch.)")
            return 1
        raw, source = shown.stdout, f"{ref}:{rel}"
    else:
        path = repo / rel
        if not path.is_file():
            _out("fail", f"{path} not found - run the automation stage for {args.ticket} first")
            return 1
        raw, source = path.read_text(encoding="utf-8"), str(path)

    try:
        tests = json.loads(raw)
        assert isinstance(tests, list) and all("fields" in t and "steps" in t for t in tests)
    except (ValueError, AssertionError):
        _out("fail", f"{source} is not an Xray bulk-import list")
        return 1

    print(f"source:  {source}")
    print(f"tests:   {len(tests)}  -> project {tests[0]['fields'].get('project', {}).get('key', '?') if tests else '?'}, "
          f"folder {tests[0].get('xray_test_repository_folder', '(root)') if tests else ''}")
    for t in tests:
        print(f"  - {t['fields'].get('summary', '')}  ({len(t.get('steps') or [])} steps)")
    if not args.apply:
        print("\nDry run: nothing was sent. Re-run with --apply to import these into Xray.")
        return 0
    if not xray_cloud.configured():
        _out("fail", "XRAY_CLIENT_ID / XRAY_CLIENT_SECRET are not set in .env (Xray Cloud > Settings > API Keys)")
        return 2
    try:
        result = xray_cloud.import_tests(tests)
    except xray_cloud.XrayError as exc:
        _out("fail", str(exc))
        return 1
    print(f"\nXray import: {result.get('status')}")
    for issue in (result.get("result") or {}).get("issues") or []:
        print(f"  created {issue.get('key')}  {issue.get('self', '')}")
    for err in (result.get("result") or {}).get("errors") or []:
        _out("warn", str(err))
    return 0 if result.get("status") == "successful" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from taf.config import PROJECT_ROOT, SETTINGS
    print(f"Dashboard: http://{args.host or SETTINGS.host}:{args.port or SETTINGS.port}/dashboard", flush=True)
    uvicorn.run("main:app", host=args.host or SETTINGS.host, port=args.port or SETTINGS.port,
                reload=args.reload, app_dir=str(PROJECT_ROOT))
    return 0


# -------------------------------------------------------------------- main --
def build_parser() -> argparse.ArgumentParser:
    from taf.config import LLM_PROVIDERS, TICKET_SOURCES

    parser = argparse.ArgumentParser(prog="python -m taf", description="Autonomous QA framework - terminal interface.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show INFO logs (agent events)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check this machine's setup").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("providers", help="show or switch the LLM router")
    p.add_argument("--set", choices=LLM_PROVIDERS, help="switch the default provider")
    p.add_argument("--server", help="apply to a running service, e.g. http://127.0.0.1:8000")
    p.set_defaults(fn=cmd_providers)

    p = sub.add_parser("board", help="list the ticket queue")
    p.add_argument("--source", choices=TICKET_SOURCES, help="jira (real) or seed (demo); default TICKET_SOURCE")
    p.set_defaults(fn=cmd_board)
    sub.add_parser("skills", help="list skills").set_defaults(fn=cmd_skills)

    p = sub.add_parser("run", help="run pipeline stages for one ticket")
    p.add_argument("ticket", help="ticket key, e.g. TB-601 (from Jira or data/seed_tickets.json)")
    p.add_argument("--stage", default="architect", choices=["triage", "architect", "automation", "full"],
                   help="how far to go (default: architect - no test repo needed)")
    p.add_argument("--provider", choices=LLM_PROVIDERS, help="router override for this run only")
    p.add_argument("--source", choices=TICKET_SOURCES,
                   help="where the ticket comes from: jira (real flow) or seed (demo); default TICKET_SOURCE")
    p.add_argument("--target-url", help="app under test (default: DASHBOARD_TARGET_URL)")
    p.add_argument("--summary", help="ticket summary, when the ticket is not in Jira or the seed file")
    p.add_argument("--description", help="ticket description / acceptance criteria")
    p.add_argument("--show", nargs="+", default=["triage", "plan", "spec", "report"],
                   choices=["triage", "plan", "spec", "logs", "report", "all"], help="what to print afterwards")
    p.add_argument("--json", action="store_true", help="print the final pipeline state as JSON")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="continue a run: approve/reject one waiting for approval, or pick up a crashed one")
    p.add_argument("run_id")
    decision = p.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="approve publishing (push / pull request)")
    decision.add_argument("--reject", action="store_true", help="reject: nothing is published")
    p.add_argument("--by", help="who decided (recorded in the run, report and PR)")
    p.add_argument("--note", help="reason, recorded with the decision")
    p.add_argument("--show", nargs="+", default=["report"],
                   choices=["triage", "plan", "spec", "logs", "report", "all"], help="what to print afterwards")
    p.add_argument("--json", action="store_true", help="print the final pipeline state as JSON")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("eval", add_help=False,
                       help="score agent output against evals/cases (`eval --help`; `eval compare A B`)")
    p.add_argument("eval_args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("xray", help="Xray test cases kept in the test repo")
    xsub = p.add_subparsers(dest="xray_command", required=True)
    x = xsub.add_parser("export", help="import a ticket's reviewed test cases into Xray (dry run unless --apply)")
    x.add_argument("ticket", help="ticket key, e.g. TB-601")
    x.add_argument("--ref", help="git ref to read from (default: the base branch, or the working tree in place)")
    x.add_argument("--test-repo", help="test repo path (default: TEST_REPO_LOCAL_PATH)")
    x.add_argument("--apply", action="store_true", help="really import (needs XRAY_CLIENT_ID / XRAY_CLIENT_SECRET)")
    x.set_defaults(fn=cmd_xray_export)

    p = sub.add_parser("serve", help="start the API and dashboard")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=cmd_serve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Reports contain emoji; a Windows console in a legacy code page must not crash on them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["eval"]:  # the eval runner has its own options; pass them through untouched
        import logging

        import taf.config  # noqa: F401  (configures logging first)
        logging.getLogger().setLevel(logging.WARNING)
        return cmd_eval(argparse.Namespace(eval_args=raw[1:]))
    args = build_parser().parse_args(argv)
    if args.command != "serve" and not args.verbose:
        # Agent events are logged at INFO for the service log; on a terminal the
        # phase lines below say the same thing, so keep only warnings and errors.
        import logging

        import taf.config  # noqa: F401  (configures logging first)
        logging.getLogger().setLevel(logging.WARNING)
    return args.fn(args)

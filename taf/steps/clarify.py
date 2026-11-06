"""
The clarify gate, between triage and test design: an unclear ticket stops here
before the plan, the spec and the test run spend tokens on guesses.

    questions       triage's own questions (each marked blocking or not) plus the
                    deterministic checks below, numbered Q1, Q2, ...
    blocking checks no acceptance criteria in the ticket (triage derived them), no PR
                    diff, no target URL, a route the ticket names that the page
                    contract does not know
    clarify_gate    pauses the graph (LangGraph `interrupt`) when a blocking question
                    exists or triage confidence < CLARIFY_THRESHOLD, and CLARIFY_GATE is
                    on. With the gate off the questions are recorded and reported only.

A paused run is resumed with a decision:
    {"action": "answer", "answers": {"Q1": "..."}}   continue with the answers
    {"action": "proceed"}                             continue; the questions become risks
    {"action": "reject"}                              stop and write the report
Answers reach the Test Architect and the Automation Engineer as `clarifications`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from langgraph.types import interrupt

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS, project_path
from taf.core.text import utc_now

ACTIONS = ("answer", "proceed", "reject")
DONE = {"passed", "not_required", "answered", "proceeded", "rejected"}

# What a ticket's own acceptance criteria look like: a heading, a numbered or bulleted
# list, or Given/When/Then.
_CRITERIA_RE = re.compile(r"(?im)acceptance criteria|^\s*(?:\d+[.)]|[-*•])\s+\S|^\s*(?:given|when|then)\b")
# An app route the ticket names: /tsp/itinerary, /trips/:id. Not URLs (https://...), file paths or
# regex flags (/g): a segment has at least two characters.
_ROUTE_RE = re.compile(r"(?<![\w.:/])/[a-z][a-z0-9-]+(?:/[a-z0-9:{}-]+)*(?![\w./])", re.IGNORECASE)


def _q(text: str, blocking: bool, source: str, criterion_ref: str = "") -> Dict[str, Any]:
    return {"text": text, "blocking": blocking, "criterion_ref": criterion_ref, "source": source}


def routes_in(text: str) -> List[str]:
    return list(dict.fromkeys(m.group(0).rstrip("/") for m in _ROUTE_RE.finditer(text or "")))


def _contract_routes() -> Optional[List[str]]:
    """Routes the page contract lists, or None when there is no contract (or it lists none)."""
    if not SETTINGS.page_contract_path:
        return None
    try:
        text = project_path(SETTINGS.page_contract_path).read_text(encoding="utf-8")
    except OSError:
        return None
    # Import paths ('../pages/x.page') are not routes.
    found = [r for r in routes_in(text) if not r.endswith((".page", ".ts", ".js"))]
    return found or None


def check_questions(state: QAState) -> List[Dict[str, Any]]:
    """Deterministic blocking checks on the ticket and the gathered context."""
    out: List[Dict[str, Any]] = []
    description = state.get("description") or ""
    criteria = (state.get("triage_analysis") or {}).get("acceptance_criteria") or []
    if not _CRITERIA_RE.search(description):
        out.append(_q(f"The ticket states no acceptance criteria; the {len(criteria)} criteria triage listed were "
                      "derived from the description and the diff. Are they what the ticket means?", True, "check"))
    if not (state.get("github_diff") or "").strip():
        prs = ", ".join(f"#{p.get('number')}" for p in state.get("pull_requests") or []) or "none linked"
        out.append(_q(f"No PR diff was found for the ticket (linked PRs: {prs}). Which pull request implements it?",
                      True, "check"))
    if not (state.get("target_url") or "").strip():
        out.append(_q("No target URL is set for this run. Which environment should the tests run against?",
                      True, "check"))
    known = _contract_routes()
    if known is not None:
        # API endpoints are not pages: the page contract does not list them.
        named = [r for r in routes_in(f"{state.get('summary', '')}\n{description}") if not r.startswith("/api/")]
        unknown = [r for r in named if not any(r == k or r.startswith(k + "/") for k in known)]
        if unknown:
            out.append(_q(f"The ticket names route(s) {', '.join(unknown)} that the page contract does not list. "
                          "Are they new, renamed, or a different app?", True, "check"))
    return out


def collect_questions(state: QAState) -> List[Dict[str, Any]]:
    """Triage's questions and the deterministic checks, numbered Q1, Q2, ..."""
    triage = state.get("triage_analysis") or {}
    questions = [_q(str(q.get("text") or ""), bool(q.get("blocking")), "triage", str(q.get("criterion_ref") or ""))
                 for q in triage.get("questions") or [] if isinstance(q, dict) and q.get("text")]
    questions += check_questions(state)
    return [{"id": f"Q{n}", **q} for n, q in enumerate(questions, start=1)]


def confidence(state: QAState) -> float:
    try:
        return float((state.get("triage_analysis") or {}).get("confidence", 1.0))
    except (TypeError, ValueError):
        return 1.0


def pause_reasons(questions: List[Dict[str, Any]], conf: float) -> List[str]:
    reasons = []
    blocking = [q for q in questions if q["blocking"]]
    if blocking:
        reasons.append(f"{len(blocking)} blocking question(s)")
    if conf < SETTINGS.clarify_threshold:
        reasons.append(f"triage confidence {conf:.2f} < CLARIFY_THRESHOLD {SETTINGS.clarify_threshold:.2f}")
    return reasons


def clarify_request(state: QAState) -> Dict[str, Any]:
    """What the person answering sees (dashboard, CLI, interrupt payload)."""
    questions = collect_questions(state)
    conf = confidence(state)
    return {"kind": "clarification", "ticket_id": state["ticket_id"], "questions": questions,
            "confidence": conf, "threshold": SETTINGS.clarify_threshold,
            "reasons": pause_reasons(questions, conf)}


def validate_decision(decision: Any) -> Dict[str, Any]:
    """Normalise a resume decision; raises ValueError when it cannot be used."""
    if not isinstance(decision, dict):
        raise ValueError("a clarification decision is an object with an 'action'")
    action = str(decision.get("action") or ("answer" if decision.get("answers") else "")).lower()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    answers = decision.get("answers") or {}
    if isinstance(answers, list):  # [{"id": "Q1", "answer": "..."}]
        answers = {str(a.get("id") or a.get("question") or ""): a.get("answer") for a in answers if isinstance(a, dict)}
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object {\"Q1\": \"...\"} or a list of {id, answer}")
    answers = {str(k).strip(): str(v).strip() for k, v in answers.items() if str(v or "").strip()}
    if action == "answer" and not answers:
        raise ValueError("no answers given - answer at least one question, or use proceed / reject")
    return {"action": action, "answers": answers, "by": str(decision.get("by") or "unknown")[:100],
            "note": str(decision.get("note") or "")[:500]}


@agent_node("Clarify Gate", engine="human", abort_on_error=False, span_type="guardrail")
def clarify_gate(state: QAState) -> Dict[str, Any]:
    if (state.get("clarify") or {}).get("status") in DONE:
        return {}
    request = clarify_request(state)
    questions, conf = request["questions"], request["confidence"]
    record: Dict[str, Any] = {"questions": questions, "confidence": conf,
                              "threshold": SETTINGS.clarify_threshold, "at": utc_now()}
    if not request["reasons"]:
        return {"clarify": {**record, "status": "passed"}}
    if not SETTINGS.clarify_gate:
        return {"clarify": {**record, "status": "not_required",
                            "note": "CLARIFY_GATE=false - " + "; ".join(request["reasons"]) + ", not asked"}}
    # Pauses the run here; the checkpoint keeps everything until a decision arrives.
    decision = validate_decision(interrupt(request))
    record.update({"by": decision["by"], "note": decision["note"], "reasons": request["reasons"]})
    if decision["action"] == "reject":
        return {"clarify": {**record, "status": "rejected"}}
    if decision["action"] == "proceed":
        risks = [q["text"] for q in questions if q["blocking"]]
        return {"clarify": {**record, "status": "proceeded", "risks": risks}}
    by_id = {q["id"]: q for q in questions}
    answered = [{"id": qid, "question": by_id[qid]["text"], "answer": text}
                for qid, text in decision["answers"].items() if qid in by_id]
    unanswered = [q["text"] for q in questions if q["blocking"] and q["id"] not in decision["answers"]]
    return {"clarify": {**record, "status": "answered", "risks": unanswered}, "clarifications": answered}


def clarifications_block(state: QAState) -> str:
    """The team's answers (and questions left open as risks) for the architect and automation prompts."""
    answers = state.get("clarifications") or []
    risks = (state.get("clarify") or {}).get("risks") or []
    if not answers and not risks:
        return ""
    lines = ["", "", "## Clarifications from the team",
             "These answers come from a person and override anything the ticket or triage assumed."]
    lines += [f"- {a['question']}\n  Answer: {a['answer']}" for a in answers]
    if risks:
        lines += ["", "Still open (treat as risks; do not invent the answer):"] + [f"- {r}" for r in risks]
    return "\n".join(lines)

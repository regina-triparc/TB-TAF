"""
LLM-as-a-judge, for the qualities rules cannot check (see judges/*.md).

Judge prompts are versioned files - `scenario_quality.v1.md` - and never edited in
place: a changed rubric is a new version, so scores stay comparable. Every result
records the prompt version, its hash, and the judge's model and provider.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

JUDGES_DIR = Path(__file__).parent / "judges"
DEFAULT_JUDGE = "scenario_quality.v1"
DIMENSIONS = ("criteria_fidelity", "scenario_relevance", "scenario_correctness",
              "spec_validates_requirements", "ambiguity_handling")


class Score(BaseModel):
    score: int = Field(ge=1, le=5)
    rationale: str


class Verdict(BaseModel):
    criteria_fidelity: Score
    scenario_relevance: Score
    scenario_correctness: Score
    spec_validates_requirements: Score
    ambiguity_handling: Score


def judge_prompt(name: str = DEFAULT_JUDGE) -> Dict[str, str]:
    text = (JUDGES_DIR / f"{name}.md").read_text(encoding="utf-8")
    return {"name": name, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()[:12]}


def judge(state: Dict[str, Any], provider: str, name: str = DEFAULT_JUDGE) -> Dict[str, Any]:
    """Score one case's outputs. Uses the router's `review` route of `provider` (budget-guarded)."""
    from taf.llm.router import ROUTER

    prompt = judge_prompt(name)
    route = ROUTER.route("review", provider)
    if route.provider == "offline":
        return {"skipped": "offline provider has no judge", "prompt": name}
    content = f"""# Ticket {state.get('ticket_id')}: {state.get('summary', '')}
{state.get('description', '') or '(empty description)'}

## Extracted acceptance criteria and questions
{json.dumps({k: (state.get('triage_analysis') or {}).get(k) for k in ('acceptance_criteria', 'gaps_and_questions')}, indent=2)}

## Test plan
{json.dumps(state.get('test_cases') or [], indent=2)[:30000]}

## Generated spec
```typescript
{(state.get('generated_typescript_code') or '(none)')[:30000]}
```"""
    verdict = ROUTER.structured(state, route, [SystemMessage(content=prompt["text"]), HumanMessage(content=content)],
                                Verdict)
    assert isinstance(verdict, Verdict)
    scores = {d: getattr(verdict, d).score for d in DIMENSIONS}
    return {
        "prompt": name, "prompt_sha256": prompt["sha256"], "model": route.model, "provider": route.provider,
        "scores": scores, "mean": round(sum(scores.values()) / len(scores), 2),
        "rationales": {d: getattr(verdict, d).rationale for d in DIMENSIONS},
    }

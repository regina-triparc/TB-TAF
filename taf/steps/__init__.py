"""
Deterministic workflow steps: plain Python around the agents, no model decides here.

    context.py      PR diff for the ticket; the test repository's conventions (read-only)
    clarify.py      the clarify gate after triage: an unclear ticket pauses for answers
    validation.py   static checks on the generated spec, then the Playwright run
    review.py       rule-based review of the spec (plus an optional LLM review)
    publish.py      human approval gate; branch, commit, push and pull request
"""

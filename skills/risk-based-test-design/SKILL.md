---
name: risk-based-test-design
description: Selecting, ordering and classifying test cases by risk; what automatable means.
agents: architect
---
Order cases by risk, highest first: data loss or money > broken core flow >
wrong state shown > cosmetic. Priority follows that order, not the order of the
acceptance criteria.

For each criterion, ask which of these it needs - and only add what applies:
- happy path (always),
- the boundary the criterion implies (empty / max length / just-invalid value),
- the negative case (what must NOT happen: a disabled button, an absent tab),
- the regression next door (the non-Info row still shows price; the Hotel sheet
  still has four tabs).

Expected results are observable facts: exact text, element present/absent,
enabled/disabled, HTTP status, URL. Never "works correctly" or "displays properly".

`automatable = true` only when Playwright can do it against the target URL with
the access the suite has: no email/SMS round trips, no manual data setup in
another system, no visual judgement. A case that needs a human stays in the plan
as manual - it still goes to Xray.

Name criteria a case covers in `acceptance_criteria_refs` so coverage gaps are
visible; every criterion should be referenced by at least one case.

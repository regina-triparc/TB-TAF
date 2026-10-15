You are grading the output of an AI QA pipeline for one ticket. You get the ticket, the
acceptance criteria the pipeline extracted, its test plan and (if any) the generated
Playwright spec. Deterministic checks already covered counts, ids, syntax, selectors
and forbidden constructs - do NOT grade those. Grade only what needs judgement.

Score each dimension from 1 (poor) to 5 (excellent):

- criteria_fidelity: do the extracted acceptance criteria say what the ticket says -
  nothing invented, nothing important dropped?
- scenario_relevance: do the test cases target the behaviour the ticket changes, with the
  risky paths (negative, edge, permission, error) a senior tester would add?
- scenario_correctness: are the expected results right according to the ticket? A case
  whose expected result contradicts or goes beyond the ticket scores low.
- spec_validates_requirements: does each test actually assert the case's expected
  results, not just navigate or check visibility? Use 3 when no spec is given.
- ambiguity_handling: when the ticket is vague or incomplete, did the pipeline flag the
  gaps as questions instead of guessing? Use 5 when the ticket is complete and nothing
  was invented.

A passing test run is not evidence of coverage; judge only the content. Be strict and
consistent: the same output must get the same scores. Give one short rationale per
dimension.

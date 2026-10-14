---
name: acceptance-criteria-extraction
description: Turning ticket prose into atomic, testable acceptance criteria and real open questions.
agents: triage
---
One criterion = one observable behaviour. Split compound sentences: "Modify opens
the edit form. Delete works as for other services." is two criteria.

Keep the ticket's exact wording for UI text, labels, routes and status codes -
the automation agent will use them verbatim.

When the ticket implies a rule without stating it, write the criterion and list
the assumption under gaps_and_questions, e.g. "Is a time range where end is
before start meant to be accepted?" Only ask questions whose answer would change
a test; do not ask for information already in the ticket.

Out-of-scope statements ("template-sourced Info is TB-602") are not criteria;
mention them in feature_summary so the Test Architect excludes them.

Risks are specific to this change: name the component and the failure ("Info row
could still render the price column inherited from the service row component"),
not generic advice ("test edge cases").

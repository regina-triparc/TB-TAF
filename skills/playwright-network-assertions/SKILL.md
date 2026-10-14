---
name: playwright-network-assertions
description: Asserting the API call behind a UI action - method, path and status - without mocking.
agents: automation
---
When an acceptance criterion names an API call ("Add calls POST .../add-info-service
and returns 202"), assert it through the UI action that triggers it. Start waiting
BEFORE the action, then await both:

```ts
const responsePromise = page.waitForResponse(r =>
  r.request().method() === 'POST' && /\/add-info-service$/.test(new URL(r.url()).pathname));
await page.getByRole('button', { name: 'Save' }).click();
const response = await responsePromise;
expect(response.status()).toBe(202);
```

- Match on method and path, not the full URL - ids and hosts vary per environment.
- Use a RegExp for path segments that contain ids (`/services\/[^/]+\/replace-info$/`).
- Assert the request body when the criterion is about what is sent:
  `expect(response.request().postDataJSON()).toMatchObject({ title: 'QA Info' })`.
- Do not mock (`page.route`) the endpoint under test - that tests the mock.
  Mocking is for third-party calls the criterion does not cover.

"Unchanged" criteria (price, totals): read the value from the UI before the
change, perform the change, and assert the same text afterwards.

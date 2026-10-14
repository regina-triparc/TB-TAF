---
name: playwright-assertions-and-waiting
description: Web-first assertions, explicit timeouts for slow pages, and no fixed sleeps.
agents: automation
---
Assert with web-first assertions - they retry until the condition holds or the
timeout expires:

```ts
await expect(locator).toBeVisible();
await expect(locator).toHaveText('Details');
await expect(locator).toBeDisabled();
await expect(page.getByRole('tab')).toHaveText(['Details', 'Travellers']); // exact, ordered list
await expect(page).toHaveURL(/\/itinerary$/);
```

Never `page.waitForTimeout(...)`, never `expect(await locator.isVisible()).toBe(true)`
(it checks once and does not retry), never `setTimeout` sleeps.

Absence: `await expect(locator).toHaveCount(0)` or `.toBeHidden()` - not a
`count()` compared to zero.

Slow pages: when the app shows a loading state, wait for it to disappear with a
generous, explicit timeout on that one assertion, rather than raising the global
timeout:

```ts
await expect(page.getByText(/Loading trip/i)).toBeHidden({ timeout: 180_000 });
```

State after an action: assert the visible consequence (row appears, sheet shows
the new value, button re-enabled) - not that the click "worked". For "updates
without a reload", assert the new state, then assert the URL did not change.

Soft assertions (`expect.soft`) are for collecting several independent checks on
one screen; a check that later steps depend on must be a hard assertion.

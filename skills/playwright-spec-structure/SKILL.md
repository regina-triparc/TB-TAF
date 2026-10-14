---
name: playwright-spec-structure
description: Spec layout - describe/step, independent tests, authentication via storageState, test data.
agents: automation
---
Layout:

```ts
import { test, expect } from '@playwright/test';

const BASE_URL = process.env.BASE_URL ?? 'https://app.example.test';

test.describe('TB-123: <summary>', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
  });

  test('[TC-001] <summary>', async ({ page }) => {
    await test.step('Open the itinerary', async () => { /* ... */ });
    await test.step('Add an Info service', async () => { /* ... */ });
  });
});
```

- One `test()` per test case; the `[TC-xxx]` prefix links the result to Xray.
- One `test.step()` per plan step, named after the step's action.
- Shared navigation goes in `beforeEach`. Helpers go in plain functions at the
  top of the file - not in a page-object class for a single spec.

Independence: every test must pass alone and in any order. A test that creates
data cleans it up (or uses a unique name such as `QA ${Date.now()}`) so re-runs
do not collide. Never rely on another test having run first.

Authentication: assume the project signs in once in a `setup` project and shares
`storageState`. Do not log in inside the spec. If a test needs credentials that
may be absent, read them from `process.env` and skip:

```ts
test.skip(!process.env.TBA_E2E_EMAIL, 'TBA_E2E_EMAIL not set');
```

Destructive cases (delete, submit payment) only against data the test created.

Mark a case the spec cannot implement honestly with `test.fixme(true, 'why')`
instead of an empty or always-passing test.

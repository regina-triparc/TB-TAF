# Page contract - TripBuilder TSP itinerary

Captured from the live portal on 2026-10-13 with Playwright's `ariaSnapshot()`.
These are REAL roles and accessible names. Use them exactly; do not invent others.

## Navigation (the app does NOT open on the itinerary)

1. `await page.goto('/')` lands on the trips dashboard (`/trips`), heading
   "My Quotes and Bookings".
2. Trip rows have NO link. The row itself is the click target:
   `page.getByRole('row', { name: /Best Of Bangkok/ }).first().click()`
3. That navigates to `/tsp/<tripId>/itinerary` and shows "Loading trip..." for
   up to ~60s. Wait for it:
   `await expect(page.getByText(/Loading trip/i)).toBeHidden({ timeout: 180000 })`
4. Trips whose "Added services" cell contains `Info` already have Info rows:
   "Best Of Bangkok", "Lima: Heritage & Flavors", "Barcelona Cultural Discovery".

## Itinerary structure

- Day heading: `paragraph` with text `Day 3 · Dec 3` (middle dot separator).
- Per-day add control: `button` named `add Add Service` - one per day card, so
  always scope or use `.nth(i)` / `.first()`.
- Every service row IS a button whose accessible name concatenates its parts.

## Service rows - the key distinction

An Info row's name contains `Info only`:

    button "The Above Riva Thai Info only Modify Delete"
      text: "The Above Riva Thai Info only"
      button "Modify"
      button "Delete"

A priced (non-Info) row carries a price/status token instead:

    button "BlackLane - Premium (Arrival) Live price Modify Delete"
      button "Modify"
      button "Delete"

So:
- all Info rows:      `page.getByRole('button', { name: /Info only/ })`
- a priced row:       `page.getByRole('button', { name: /Live price/ })`
- Modify on a row:    `row.getByRole('button', { name: 'Modify' })`
- Delete on a row:    `row.getByRole('button', { name: 'Delete' })`

An Info row must NOT contain `Live price`, `Check failed`, `Needs recheck` or
`Recheck`. Assert that by checking the row's accessible name, not by looking for
a `cell` - these are buttons, not table cells.

Real Info titles present on "Best Of Bangkok": "The Above Riva Thai",
"Local Eatery", "Mae Klong Market Eatery", "Yaowarat Street Stalls",
"Hotel Restaurant", "Day 6 info".

## Other controls on the page

- `button "Send quote"`
- `button "check Day-by-day"` (pressed) and `button "Services"` - view toggles
- Header: `button "dark_mode"`, `button "logout"`

## Known environment defect

The activities micro-frontend is blocked by Content Security Policy:
`triparc-activities-dev-mfe.azurewebsites.net/remoteEntry.json` violates
`connect-src`. Activity remotes therefore fail to load. Info rows are rendered by
the shell and are unaffected, but do not write tests that depend on Activity
services rendering.

## Rules for writing the spec - these correct real mistakes made on earlier attempts

1. **Day headings are `paragraph`, never `row`.** There is no `row` role anywhere on
   the itinerary and no `cell` role either - every service is a `button`. A previous
   attempt used `getByRole('row', { name: /Day 3/ })` and timed out after 180s.
   `getByRole('row', ...)` is ONLY valid on the `/trips` dashboard.

2. **Several Info rows exist on one trip (6 on "Best Of Bangkok").** Any locator for
   `/Info only/` matches them all and Playwright fails with a strict-mode violation.
   Always end such a locator with `.first()`.

3. **Modify and Delete ARE visible on every Info row.** Never assert they are hidden.
   A previous attempt asserted `Modify` was hidden and then clicked it.

4. **Do not click "Add Service" to inspect a row that already exists.** The trip
   already contains Info rows; just assert against them after the trip loads.

5. **Assert on the row's accessible name**, which concatenates its parts. An Info
   row's name contains `Info only` and must NOT contain `Live price`,
   `Check failed`, `Needs recheck` or `Recheck`. For example:
   `await expect(infoRow).toHaveAccessibleName(/Info only/)`.

6. **Keep it minimal.** One `test.describe` and the given test cases only. No
   `test.step` wrappers, no helper functions, no loops.

7. **Every test must navigate first** using the three steps in "Navigation" above:
   `goto('/')`, click the trip row, wait for "Loading trip" to be hidden with a
   180000ms timeout. Put that in a `beforeEach`.

## A correct minimal example - follow this shape exactly

```ts
import { test, expect } from '@playwright/test';

test.describe('TB-601: Info services', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.getByRole('row', { name: /Best Of Bangkok/ }).first().click();
    await expect(page.getByText(/Loading trip/i)).toBeHidden({ timeout: 180000 });
  });

  test('[TC-001] an Info row shows the Info only badge with Modify and Delete', async ({ page }) => {
    const infoRow = page.getByRole('button', { name: /Info only/ }).first();
    await expect(infoRow).toBeVisible();
    await expect(infoRow).not.toHaveAccessibleName(/Live price|Needs recheck|Check failed/);
    await expect(infoRow.getByRole('button', { name: 'Modify' })).toBeVisible();
    await expect(infoRow.getByRole('button', { name: 'Delete' })).toBeVisible();
  });
});
```

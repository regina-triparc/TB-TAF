---
name: spec-authoring-rules
description: Keep the spec lean, take option values from the diff, and name and scope every role locator.
agents: automation
---
Rules for the generated `e2e/qa/<TICKET>.spec.ts`. They override any example that contradicts them.

## 1. The spec holds tests, not utilities

- No top-level `function` declarations in the spec. Reuse the page objects, fixtures and
  helpers listed under "Test repository conventions" (for example `TripItineraryPage`:
  `openServiceDetail`, `openModifyByName`, `deleteServiceByName`, `closeServiceDetail`).
  Never re-implement one of those methods under a new name.
- A step a listed page object does not cover is written inline in the test, with the locator
  in a `const` at the top of that test.
- When the same inline steps appear in 3+ tests, keep them inline and list the missing method
  once at the top of the file, so a person can move it into a page object:
  `// TODO(page-object): InfoFormPage.fill({ title, start, end, description })`
- Allowed at top level: imports, `BASE_URL`, constants for test data, `test.describe`.

## 2. Values come from the live snapshot, the diff or the page contract, never from the plan's wording

Look in this order: the "Live page snapshot" section (captured for this run), the
verified page objects, the diff, the page contract.

The plan describes values in human words ("6:30 PM"). The UI may show them differently.
Before writing a step that picks a value (time, date, currency, enum, dropdown option):

1. Look in the diff for the code that builds the options (an interval loop, an enum, a
   format string such as `HH:mm`, a `ShoppingType` union) or for them in the page contract.
2. Write the exact values the UI shows in one comment at the top of the file:
   `// OPTIONS (from diff/contract): Start/End = "00:00".."23:45" every 15 min, 24-hour HH:mm`
3. Convert each plan value into that format: plan "6:30 PM" becomes option `'18:30'`.
4. If neither the diff nor the contract shows the options, do not guess a label. Open the
   control, take a real option, and assert later against the label you read:
   ```ts
   // ASSUMPTION: Start options unknown; the test uses whichever option is listed second.
   await form.getByRole('combobox', { name: 'Start' }).click();
   const option = form.getByRole('listbox', { name: 'Start' }).getByRole('option').nth(1);
   const startLabel = (await option.innerText()).trim();
   await option.click();
   ```

The format you TYPE or PICK can differ from the format the app DISPLAYS: a picker may take
`'18:30'` while the detail view shows "6:30 PM". Assert each in its own format.

The same applies to dates and positions: do not map "Day 3" to `.nth(2)` unless the
contract says what every match of that locator is. Locate the day by its label instead.

Request bodies: take field names from the diff's types (e.g. an `InfoServiceBody`
interface), never from the plan's wording - the plan's "description" may be `descriptionHtml`.

## 3. Every role locator has a name and a scope

- `getByRole(role)` without `{ name }` is only allowed for counting inside an already scoped
  container. Never call it on `page` directly.
- Roles that repeat on a page (`tab`, `tablist`, `dialog`, `option`, `combobox`, `button`,
  `textbox`) start from the nearest named container, usually the open `dialog`:
  ```ts
  const sheet = page.getByRole('dialog', { name: title });
  await expect(sheet.getByRole('tab')).toHaveText(['Details', /^Travellers/]);
  ```
  `page.getByRole('tab')` also matches the trip-level tabs (Overview, Itinerary, Travellers…)
  and fails.
- Choose names that appear in the page contract or the diff. A name that appears in neither is
  an assumption: add `// ASSUMPTION: <role> "<name>" not in contract` next to it.
- Close or cancel a modal through its own named button, scoped to that dialog:
  `dialog.getByRole('button', { name: 'Close' })`. Do not expect every flow to have "Cancel".

## 4. Data the test depends on

- Create what the test asserts on and clean it up in the same test. If cleanup runs after an
  assertion that can fail, use `test.afterEach` with the created titles, not a final step.
- Read values the test compares (price, commission, totals) from a locator named in the page
  contract. If the contract has none, `test.fixme(true, 'no locator for <value> in contract')`
  instead of guessing `getByText(/Total/)`.
- Do not hard-code a year or a date: derive the date from the day label the test clicked.

## 5. Before you return the file

Check that every `getByRole` call has `{ name }` or a scoped parent. Check that every literal
passed to an option, a date or a status comes from the OPTIONS comment or the contract.
Check that each `// ASSUMPTION:` names what you could not find, not what you hope is there.

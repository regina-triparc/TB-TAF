---
name: playwright-locators
description: Locator priority, scoping and strictness for resilient Playwright selectors.
agents: automation
---
Choose locators in this order; take the first that uniquely identifies the element:

1. `page.getByRole(role, { name })` - buttons, links, headings, rows, tabs, dialogs, textboxes.
2. `page.getByLabel('Title')` - form fields with a visible label.
3. `page.getByPlaceholder(...)`, `page.getByText(...)` - only for non-interactive text.
4. `page.getByTestId('...')` - when the app ships stable test ids.
5. CSS / XPath - never, unless the page contract gives no alternative; add `// ASSUMPTION:`.

Names: pass a string for an exact accessible name, a RegExp when the name is
composed of several parts (`/Info only/`). Accessible names often concatenate
child text - a row button may be named "Riva Thai Info only Modify Delete".

Strictness: a locator that matches several elements fails on action. Scope it
instead of reaching for `.first()`:

```ts
const day3 = page.getByRole('region', { name: /Day 3/ });   // or a parent locator
await day3.getByRole('button', { name: 'Add Service' }).click();
```

Use `.first()` / `.nth(i)` only when the elements are genuinely interchangeable,
and say so in a comment. Use `.filter({ hasText })` / `.filter({ has })` to narrow
a list to the one item a test is about.

Never build selectors from generated class names (`.css-1x2y3z`, `.sc-abc`) or
DOM position (`div > div:nth-child(3)`); they change on every build.

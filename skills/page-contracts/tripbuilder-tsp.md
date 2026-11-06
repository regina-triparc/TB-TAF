# Page rules - TripBuilder TSP

These are rules for driving the app and treating its data. There are deliberately no
selectors, accessible names, trip names, service names, dates or counts in this file:
the UI and the shared account change every week, and anything written here goes stale.

## Where the concrete details come from

1. **Page objects** (listed under "Test repository conventions", with their import paths).
   They own every selector. Use them for anything they cover and never re-implement them
   in the spec.
2. **The live page snapshot** (captured for this run). Use it for real roles, accessible
   names, option values and display formats of anything the page objects do not cover.
   When it disagrees with a page object, the snapshot is newer: follow it and add
   `// ASSUMPTION: page object out of date - <what differs>`.
3. **Never** from memory, from examples in the ticket, or from earlier specs. A name you
   have not seen in the snapshot or a page object does not exist.

The snapshot was taken on whichever trip the explorer opened. Its trip, service, hotel and
traveller names, its dates, prices and counts are sample data that show the structure.

## Test data

1. **Never hard-code data.** No trip names, service or hotel names, traveller names, day
   numbers, dates, years, prices or counts in a spec. Read them from the page at run time
   through the page objects.
2. **Open the QA trip with `openTrip()` and no argument.** The test repository decides
   which trip that is (`TB_QA_TRIP`, else the first trip on the dashboard).
3. **Pick days from the trip, not by number:** `const [day] = await itinerary.dayLabels();`.
   Use a later day only when the case needs two days, and `test.skip` with a reason when
   the trip has fewer days than the case needs.
4. **Find existing items by kind, not by name.** When a case needs "a priced service" or
   "a hotel", take the first one the page object finds on the trip. When the trip has
   none, the case cannot run: write `test.fixme(true, 'needs a trip with <what>')`.
5. **Create what a test changes, with a unique title** (`${PREFIX} <case> ${Date.now()}`),
   and delete it in `afterEach` (`deleteInfosStartingWith(PREFIX)`). A cleanup step at the
   end of a test never runs when an assertion fails.
6. **A test creates the trip it asserts on.** A check on trip-level data - the party
   (adults, children), rooms, trip dates, prices, status - needs its own trip with known
   values, created in setup and deleted after. There is no trip-creation helper yet:
   write such a test as `test.fixme(true, 'needs a trip-creation setup helper (party,
   rooms, dates)')` with its setup and checks as comments. Never build a trip with raw
   locators, and never assert those values on the QA trip - not even "unchanged before and
   after": the QA trip may have no price at all. The review rejects a test that reads
   price, commission or status without `test.fixme`.
7. **Dates come from the page.** Expect the date the page object reads for the day you
   used (`dayDate(day)`, `dayIsoDate(day)`); never a literal.
8. **Values from the ticket are requirements, not fixtures.** Exact copy the acceptance
   criteria state (tab names, tile names, an error message) may be asserted literally.
   Example data in the ticket ("Dec 3", "Sunset drinks", a hotel name) may not.

## Driving the app

1. The app opens on a dashboard, not on a trip; `openTrip()` handles the navigation and
   waits until the itinerary has finished loading. Never wait with a fixed timeout.
2. A dialog or panel can repeat names that also exist on the page behind it (tabs,
   buttons). Scope every locator to the dialog or panel it belongs to.
3. Several items can share text. Target the one your test created by its unique title;
   use `.first()` only when any one will do.
4. An input's value and the text shown for it elsewhere can use different formats (a time
   picker's option vs the time shown on a detail sheet). Take option values from the
   snapshot's dropdown and the displayed format from the snapshot of the screen that shows
   it, and assert each where it appears.
5. Writes are asynchronous: assert the response the page object's submit returns, then
   the UI update, without reloading the page unless the case is about a reload.
6. Prefer the page objects' open/submit helpers over clicking through dialogs yourself;
   they wait for the right moment.
7. **Never assume a URL.** Routes change with the UI. To prove "no reload", keep
   `const url = page.url()` from before the action and expect `toHaveURL(url)` after; never
   expect a literal path. To check where a click leads, assert on what the page shows.
8. **Rendered text is not clean text.** It carries whitespace around it (`" Travellers "`),
   and on cards the icon names run straight into the labels (`info_outline<title>Info
   delete_outline`). Assert with plain strings - `toHaveText` / `toContainText` trim and
   collapse whitespace for strings - and never with `^`, `$` or `\b` on rendered text. For
   what a card says about itself (its type, its status), use the page object or the details
   panel, not the card's inner text.
9. **Rich text is saved with `&nbsp;`.** The description editor stores typed spaces as
   `&nbsp;` (`<p>QA&nbsp;bold&nbsp;part.</p>`). Before comparing saved HTML or a request
   body, normalise: `html.replace(/&nbsp;| /g, ' ')`. Compare tags and text, not bytes.

## Keep it minimal

One `test.describe`, the given test cases, no helper functions in the spec.

## Shape of a spec

```ts
import { test, expect } from '@playwright/test';
import { TripItineraryPage } from '../pages/trip-itinerary.page';

const PREFIX = 'QA TB-601';

test.describe('TB-601: Info services', () => {
  let itinerary: TripItineraryPage;

  test.beforeEach(async ({ page }) => {
    itinerary = new TripItineraryPage(page);
    await itinerary.openTrip();
  });

  test.afterEach(async () => {
    await itinerary.deleteInfosStartingWith(PREFIX);
  });

  test('[TC-002] adding an Info from a day shows it with its times on the sheet', async () => {
    const [day] = await itinerary.dayLabels();
    const title = `${PREFIX} add ${Date.now()}`;
    const form = await itinerary.openAddInfo(day);
    await expect(form.date).toHaveValue(await itinerary.dayIsoDate(day));
    await form.fill({ title, start: '18:30', end: '20:00' }); // option values from the snapshot's dropdown
    expect((await form.submit()).status).toBe(202);
    await expect(itinerary.infoRow(title)).toHaveCount(1);

    const sheet = await itinerary.openInfoSheet(title);
    await expect(sheet.schedule).toContainText(await itinerary.dayDate(day));
  });

  test('[TC-003] the trip header counts every traveller and room', async () => {
    test.fixme(true, 'needs a trip-creation setup helper (party, rooms, dates)');
    // setup: create a trip with a known party and rooms; afterEach: delete it.
    // expect: the trip header shows that party and those rooms.
  });
});
```

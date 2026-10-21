# Page contract - TripBuilder TSP itinerary

Captured from the live UAT portal with Playwright's `ariaSnapshot()`; first on 2026-10-13,
re-verified on 2026-10-21. These are REAL roles and accessible names. Use them exactly.
When a "Live page snapshot" section is also in the prompt, it is newer and wins.

## Use the page objects first

The test repository has page objects that wrap everything below and were checked against
UAT on 2026-10-21. Prefer them to raw locators, and never re-implement them in the spec:

- `TripItineraryPage` (`e2e/pages/trip-itinerary.page.ts`): `openTrip(/Best Of Bangkok/)`,
  `dayCard('Day 3')`, `dayTitle('Day 3')`, `addServiceButton('Day 3')` / `addServiceButton()`
  (toolbar), `pickerTile('Info')`, `openToolbarAddService(3)`, `dayPicker`,
  `openAddInfo('Day 3')`, `openEditInfo(title)`, `openInfoSheet(title)`, `infoRow(title)`,
  `pricedRow('Waldorf Astoria')`, `tripStatus`, `priceAndCommission()`,
  `deleteServiceByName(name)`, `deleteInfosStartingWith(prefix)`.
- `InfoFormPage` (`e2e/pages/info-form.page.ts`): `dialog`, `title`, `date`, `description`,
  `timeInput('Start')`, `pickTime('Start', '18:30')`, `setDescription(text)`, `fill({...})`,
  `submitButton`, `submit()` (returns `{ method, status, url, body }`), `cancel()`.
- `InfoSheetPage` (`e2e/pages/info-sheet.page.ts`): `dialog`, `tabs`, `tab(name)`, `schedule`,
  `description`, `close()`.

## Navigation (the app does NOT open on the itinerary)

1. `page.goto('/')` lands on the trips dashboard (`/trips`).
2. Trip rows have NO link; the row is the click target:
   `page.getByRole('row', { name: /Best Of Bangkok/ }).first().click()`
3. That opens `/tsp/<tripId>/itinerary`. "Loading trip..." can still be absent when the
   wait starts, so waiting for it to be hidden is NOT enough. Wait for the first
   `button "add Add Service"` instead (it renders with the days) - `openTrip()` does this.
4. Trips with Info rows: "Best Of Bangkok", "Lima: Heritage & Flavors",
   "Barcelona Cultural Discovery".

## Trip header (visible on every tab)

    group "Trip status": Price $80.55 Estimate Commission $14.50 Refining Unpaid

Price and commission are read from this group (`priceAndCommission()`). There is no
"Total" label anywhere.

## Itinerary structure

- 6 day cards (`.itin__day-card`), each titled `Day 3 · Dec 3` (`.itin__day-title`).
- `button "add Add Service"` appears N+1 times for N days:
  - the FIRST is the **toolbar** button, outside the day cards;
  - each day card's own button is the last element of that card.
  So `.nth(2)` is Day 2's button. Never use an index: `addServiceButton('Day 3')`.
- Every service row is a `button` whose accessible name concatenates its parts.

## Add Service picker (from a day card)

    dialog "Pick a service type to start shopping"
      paragraph "Day 3 · Bangkok"
      button "Close"
      button "hotel Hotel" | "flight Flight" | "local_activity Activity"
      button "directions_car Ground transfer" | "directions_boat Cruise" | "info Info"

Tile names start with an icon ligature: match `/^(\S+\s+)?Info$/` (`pickerTile('Info')`).
Picking Hotel leaves the dialog for an in-page search (heading "Find the right hotel for
your trip") with no Cancel; go back with `button "Itinerary"`.

## Toolbar flow (the first Add Service button)

    dialog "Pick a day for the new service"
      paragraph "After choosing a day, you'll pick a service type."
      button "Close"
      list > listitem > button "Day 1 ·Bangkok" ... "Day 6 ·Bangkok"

After a day is picked, the service-type picker above opens for that day.

## Add / Edit Info form

    dialog "Add Info"                      ("Edit Info" from Modify)
      button "Close"
      textbox "Title"                      placeholder "Dinner at Taberna Real"
      textbox "Date" [disabled]            "2026-12-03" when opened from Day 3
      combobox "Start" -> listbox "Start" -> 96 options "00:00" ... "23:45"
      combobox "End"   -> listbox "End"   -> same options
      .ql-editor                           rich-text description (no accessible name)
      button "Cancel"
      button "Add service"                 ("Save changes" on the edit form; disabled while Title is empty)

- **The pickers take 24-hour `HH:mm` in 15-minute steps.** There is no "6:30 PM" option;
  use `'18:30'`. The combobox is an `<input>`: assert it with `toHaveValue('18:30')`.
- **The sheet DISPLAYS 12-hour times**: "Start 6:30 PM End 8:00 PM". So a test picks
  `'18:30'` and then expects `6:30 PM` on the sheet.
- Writes, answered 202 (PR #740 `info-service.service.ts`):
  - add: `POST .../draft-trips/{tripId}/days/{dayNumber}/add-info-service`
  - edit: `PUT .../draft-trips/{tripId}/services/{serviceId}/replace-info`
  - body: `{ title, descriptionHtml, startTime: "18:30:00" | null, endTime, expectedVersion }`.
    The description field is **`descriptionHtml`**, not `description`. Quill saves typed
    spaces as `&nbsp;` (`<p>Smoke&nbsp;description</p>`): normalise before comparing.

## Service detail sheet

    dialog "<service title>"
      button "Close"                       [data-test="sds-close"]
      tablist
        tab "Details" [selected]
        tab "Travellers"                   (Hotel: Details, Booking, Travellers, T&Cs)
      [data-test="sds-schedule"]           "Date Dec 3, 2026 Start 6:30 PM End 8:00 PM"
      [data-test="sds-info-description"]   only when the Info has a description
      button "Back"

The trip page has its own tablist (`Overview`, `Itinerary`, `Travellers`, `Payment`,
`Documents`) outside the dialog, so an unscoped `page.getByRole('tab')` matches both.
Always start from the dialog (`InfoSheetPage.tabs`).

## Service rows

    button "The Above Riva Thai Info only Modify Delete"      Info row
    button "BlackLane - Premium (Arrival) check Live price Modify Delete"
    button "Waldorf Astoria Bangkok error_outline Check failed Recheck Modify Delete"   the hotel
    button "Full Day - Best of Bangkok (with Lunch) help_outline Needs recheck Recheck Modify Delete"

- An Info row's name contains `Info only` and none of `Live price`, `Check failed`,
  `Needs recheck`, `Recheck`.
- Priced rows: `/Live price|Check failed|Needs recheck/`. The hotel is "Waldorf Astoria
  Bangkok". Do not find it with `/hotel/i`: "Hotel Restaurant" is an **Info** row.
- Info titles on "Best Of Bangkok" include "The Above Riva Thai" (Day 2), "Local Eatery",
  "1/2 Day Cycling Bangkok's Countryside" (Day 3), "Market Hopping Full Day" (Day 4),
  "Hotel Restaurant", "Day 6 info". Other runs may add more.

## Known environment defect

The activities micro-frontend is blocked by Content Security Policy
(`triparc-activities-dev-mfe.azurewebsites.net/remoteEntry.json` violates `connect-src`).
Do not write tests that depend on Activity services rendering.

## Rules for writing the spec - these correct real mistakes made on earlier attempts

1. **Day headings are not `row`s.** There is no `row` role on the itinerary; use
   `dayCard('Day 3')`. `getByRole('row', ...)` is ONLY valid on the `/trips` dashboard.
2. **Several Info rows exist on one trip.** `/Info only/` matches all of them; use
   `infoRow(title)` for a specific one, `.first()` only when any one will do.
3. **Modify and Delete ARE visible on every Info row.**
4. **Create the data a test changes, with a unique title (`QA <case> ${Date.now()}`), and
   delete it in `afterEach`** with `deleteInfosStartingWith(prefix)`: a cleanup step at the
   end of the test never runs when an assertion fails, and leftovers break later runs.
5. **Every test opens the trip in `beforeEach`** with `TripItineraryPage.openTrip(/Best Of Bangkok/)`.
6. **Keep it minimal.** One `test.describe`, the given test cases, no helper functions.

## A correct example - follow this shape

```ts
import { test, expect } from '@playwright/test';
import { TripItineraryPage } from '../pages/trip-itinerary.page';

const PREFIX = 'QA TB-601';

test.describe('TB-601: Info services', () => {
  let itinerary: TripItineraryPage;

  test.beforeEach(async ({ page }) => {
    itinerary = new TripItineraryPage(page);
    await itinerary.openTrip(/Best Of Bangkok/);
  });

  test.afterEach(async () => {
    await itinerary.deleteInfosStartingWith(PREFIX);
  });

  test('[TC-002] adding an Info from Day 3 shows it with times on the sheet', async () => {
    const title = `${PREFIX} sunset ${Date.now()}`;
    const form = await itinerary.openAddInfo('Day 3');
    await expect(form.date).toHaveValue('2026-12-03');
    await form.fill({ title, start: '18:30', end: '20:00' });
    const call = await form.submit();
    expect(call.status).toBe(202);
    await expect(itinerary.infoRow(title)).toHaveCount(1);

    const sheet = await itinerary.openInfoSheet(title);
    await expect(sheet.tabs).toHaveText(['Details', 'Travellers']);
    await expect(sheet.schedule).toContainText(/Start\s*6:30 PM/); // picked 18:30, shown 12-hour
  });
});
```

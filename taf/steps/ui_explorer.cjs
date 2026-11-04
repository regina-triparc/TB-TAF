/*
 * Live UI explorer for the Automation Engineer.
 *
 *   node ui_explorer.cjs <config.json> <baseURL>      (cwd = the Playwright test repository)
 *
 * config: {storageState, entry, keywords: [...], budget, maxLines}
 *
 * Nothing about the app is written down here. The explorer opens the start page with the
 * test repository's saved session, opens one item from it (the first row whose name
 * matches `entry`, else the first data row), and then discovers what to look at: it reads
 * the page's accessibility tree, ranks the controls by the ticket's keywords, clicks the
 * best ones and records what each click revealed - a dialog, a new page, a changed panel,
 * and one level further inside a dialog, including the options of its dropdowns.
 *
 * Prints ONE JSON line: {"ok", "url", "entry", "states": [{name, ok, snapshot|error}]}.
 *
 * Read-only by construction:
 *   - every PUT / PATCH / DELETE, and every POST whose path looks like a write, is aborted;
 *   - controls that read like a submit or a destructive action are never clicked.
 * Playwright is loaded from the test repository, so no dependency is added here.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

// Every non-GET to the app is aborted unless it reads like a lookup; elsewhere only sign-in
// and telemetry calls pass. A "recheck" or "refresh" POST changes data as surely as a save.
const READ_POST = /(search|query|lookup|filter|find|autocomplete|suggest|graphql)/i;
const THIRD_PARTY_POST = /(oauth|token|authorize|telemetry|collect|track|metrics|insights)/i;
const SUBMIT_TEXT = /^(check\s+)?(add service|save( changes)?|delete|send( quote)?|confirm|submit|book|pay)\b/i;
// Leaves, destroys, or starts server work (a recheck reprices the trip even when its request is aborted).
const NEVER = /\b(remove|delete|sign ?out|log ?out|logout|cancel|close|back|discard|archive|duplicate|copy|share|download|print|help|recheck\w*|refresh|sync|publish|generate|import|export)\b/i;
const ACTION = /^(\S+\s+)?(add|open|view|edit|modify|details?|show|more)\b/i;
// `- button "Name"`, or YAML-quoted when the name has a colon: `- 'row "Lyon: Old Town"':`.
const ARIA_LINE = /^(\s*)- ('?)(button|tab|link|menuitem|combobox|row) "((?:[^"\\]|\\.)*)"/;
const MONTHS = /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b/gi;

function emit(result) {
  process.stdout.write(`\n${JSON.stringify(result)}\n`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const firstLine = (error) => String(error && error.message ? error.message : error).split('\n')[0].slice(0, 300);

/** Same control, different data: "Day 1 Sat, Nov 28, 2026" and "Day 2 Sun, Nov 29, 2026" are one template. */
function template(role, name) {
  return `${role}:${name.replace(MONTHS, 'M').replace(/\d+/g, '#').replace(/\s+/g, ' ').trim().toLowerCase()}`;
}

function summarizeOptions(text) {
  const options = [...text.matchAll(/option "([^"]*)"/g)].map((m) => m[1]);
  if (!options.length) return text;
  const head = options.slice(0, 6).join(', ');
  const tail = options.slice(-2).join(', ');
  return `${options.length} options: ${head}${options.length > 8 ? ` ... ${tail}` : ''}`;
}

function clip(text, max) {
  const lines = text.split('\n');
  return lines.length > max ? [...lines.slice(0, max), `  ... (${lines.length - max} more lines)`].join('\n') : text;
}

/** Interactive controls in an accessibility tree, in document order, with their index among same-named ones. */
function controls(snapshot) {
  const seen = new Map();
  const out = [];
  for (const line of snapshot.split('\n')) {
    const m = ARIA_LINE.exec(line);
    if (!m) continue;
    const [, , yaml, role, raw] = m;
    let name = raw.replace(/\\"/g, '"');
    if (yaml) name = name.replace(/''/g, "'");
    const key = `${role}\u0000${name}`;
    const nth = seen.get(key) || 0;
    seen.set(key, nth + 1);
    if (/\[disabled\]/.test(line)) continue;
    out.push({ role, name, nth });
  }
  return out;
}

function matches(control, keywords) {
  const name = control.name.toLowerCase();
  const escape = (k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return keywords.filter((k) => new RegExp(`(^|[^a-z0-9])${escape(k)}([^a-z0-9]|$)`, 'i').test(name));
}

function score(control, keywords) {
  let s = 3 * matches(control, keywords).length;
  if (control.role === 'tab') s += 1;
  if (ACTION.test(control.name)) s += 1;
  return s;
}

/**
 * On the page, "Add service" opens a flow; inside a dialog the same words submit it. So
 * add-like names are refused only in dialogs. Writes are aborted at the network either way.
 */
function clickable(control, inDialog) {
  if (!control.name || control.role === 'row' || control.role === 'combobox' || NEVER.test(control.name)) return false;
  return !SUBMIT_TEXT.test(control.name) || (!inDialog && /^(\S+\s+)?add\b/i.test(control.name));
}

/**
 * Highest-scoring controls first. One per template ("Day 1 ..." and "Day 2 ..." are one),
 * and one per keyword match: three cards that all match "info" show the same thing.
 */
function rank(list, keywords, limit, minScore = 0, inDialog = false) {
  const best = new Map();
  for (const c of list.filter((c) => clickable(c, inDialog))) {
    const t = template(c.role, c.name);
    const s = score(c, keywords);
    if (!best.has(t) || best.get(t).s < s) best.set(t, { ...c, s });
  }
  const seen = new Set();
  return [...best.values()].filter((c) => c.s >= minScore).sort((a, b) => b.s - a.s).filter((c) => {
    const hit = matches(c, keywords).sort().join('|');
    if (!hit) return true;
    const key = `${c.role}:${hit}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, limit);
}

async function main() {
  const [configPath, baseURL] = process.argv.slice(2);
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  const keywords = (config.keywords || []).map((k) => String(k).toLowerCase()).filter(Boolean);
  const budget = config.budget || 12;
  const maxLines = config.maxLines || 80;
  const repo = process.cwd();
  const storageState = path.resolve(repo, config.storageState || 'e2e/.auth/state.json');
  if (!fs.existsSync(storageState)) {
    return emit({ ok: false, reason: `no saved session at ${storageState} - run npm run e2e:auth-manual` });
  }
  const { chromium } = createRequire(path.join(repo, 'package.json'))('@playwright/test');
  const browser = await chromium.launch({ headless: true });
  const result = { ok: true, url: '', entry: '', states: [], blocked: [], allowed: [] };
  const record = (name, snapshot, error) => result.states.push(error ? { name, ok: false, error } : { name, ok: true, snapshot });
  try {
    const context = await browser.newContext({ baseURL, storageState, viewport: { width: 1400, height: 950 } });
    const appHost = new URL(baseURL).host;
    await context.route('**/*', (route) => {
      const request = route.request();
      const method = request.method();
      if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return route.continue();
      const url = new URL(request.url());
      const read = url.host === appHost ? READ_POST.test(url.pathname) : THIRD_PARTY_POST.test(url.pathname);
      const entry = `${method} ${url.host}${url.pathname}`;
      if (!read) {
        if (!result.blocked.includes(entry)) result.blocked.push(entry);
        return route.abort();
      }
      if (!result.allowed.includes(entry)) result.allowed.push(entry);
      return route.continue();
    });
    const page = await context.newPage();
    const signedOut = () => /login|authorize|signin|\/u\/|\/landing\b/i.test(new URL(page.url()).host + new URL(page.url()).pathname);
    const root = async () => ((await page.locator('main').count()) ? page.locator('main').first() : page.locator('body'));
    const tree = async (locator) => locator.ariaSnapshot({ timeout: 15000 }).catch(() => '');
    const dialogs = () => page.getByRole('dialog');

    /** Wait until the page stops changing: network quiet, no "Loading", the tree stable for a second. */
    const settle = async (timeoutMs = 60000) => {
      await page.waitForLoadState('networkidle', { timeout: Math.min(timeoutMs, 20000) }).catch(() => {});
      const end = Date.now() + timeoutMs;
      let last = '';
      while (Date.now() < end) {
        const now = await tree(await root());
        if (now && now === last && !/loading/i.test(now.slice(0, 400))) return;
        last = now;
        await sleep(1000);
      }
    };
    const closeDialogs = async () => {
      for (let i = 0; i < 4 && (await dialogs().count()) > 0; i += 1) {
        await page.keyboard.press('Escape');
        await sleep(300);
      }
    };
    const click = async (scope, control) => {
      const target = scope.getByRole(control.role, { name: control.name, exact: true }).nth(control.nth);
      await target.scrollIntoViewIfNeeded({ timeout: 10000 }).catch(() => {});
      await target.click({ timeout: 10000 });
      await sleep(800);
      await settle(15000);
    };
    const expandDropdowns = async (dialog, label) => {
      const boxes = controls(await tree(dialog)).filter((c) => c.role === 'combobox').slice(0, 2);
      for (const box of boxes) {
        try {
          await dialog.getByRole('combobox', { name: box.name, exact: true }).nth(box.nth).click({ timeout: 5000 });
          const list = page.getByRole('listbox').last();
          await list.waitFor({ timeout: 5000 });
          record(`${label} -> dropdown "${box.name}"`, summarizeOptions(await tree(list)));
        } catch (error) {
          record(`${label} -> dropdown "${box.name}"`, null, firstLine(error));
        }
        await page.keyboard.press('Escape');
        await sleep(300);
      }
    };

    // ---- open the app and one item from its start page --------------------------------
    try {
      await page.goto('/', { timeout: 90000 });
      await settle(90000);
    } catch (error) {
      throw new Error(`could not open the app at ${page.url() || baseURL}: ${firstLine(error)}`);
    }
    if (signedOut()) {
      return emit({ ok: false, reason: `saved session expired (landed on ${new URL(page.url()).host}) - run npm run e2e:auth-manual` });
    }
    const start = await tree(await root());
    record('Start page', clip(start, 40));
    // A table's first row is its header; the item to open is a data row.
    const rows = controls(start).filter((c) => c.role === 'row');
    const data = rows.length > 1 ? rows.slice(1) : [];
    const entryRe = config.entry ? new RegExp(config.entry, 'i') : null;
    const entry = (entryRe ? data.find((r) => entryRe.test(r.name)) : null) || data[0];
    if (entry) {
      result.entry = entry.name;
      // A click that lands before the list's handlers are bound does nothing: retry until the URL moves.
      const startUrl = page.url();
      const row = page.getByRole('row', { name: entry.name, exact: true }).nth(entry.nth);
      for (let i = 0; i < 6 && page.url() === startUrl; i += 1) {
        await row.click({ timeout: 30000 }).catch(() => {});
        await page.waitForURL((url) => url.toString() !== startUrl, { timeout: 10000 }).catch(() => {});
      }
      if (page.url() === startUrl) throw new Error(`clicking the row "${entry.name}" did not open it`);
      await settle(180000);
    }
    const itemUrl = page.url();
    result.url = itemUrl;
    const home = await tree(await root());
    record(entry ? 'Opened item (sample data - its names are examples, not fixtures)' : 'Start page (full)', clip(home, maxLines));

    const restore = async () => {
      await closeDialogs();
      if (page.url() !== itemUrl) {
        await page.goto(itemUrl, { timeout: 90000 }).catch(() => {});
        await settle(120000);
      }
    };

    // ---- explore: the controls the ticket talks about first ---------------------------
    const MAX_DEPTH = 3;
    let used = 0;
    const label = (path) => path.map((c) => `${c.role} "${c.name}"`).join(' -> ');
    // Re-open a dialog chain from the item: the page-level control, then each dialog control in turn.
    const replay = async (path) => {
      await restore();
      for (let i = 0; i < path.length; i += 1) await click(i === 0 ? page : dialogs().last(), path[i]);
    };
    // Inside a dialog: what the ticket's words point at, else the first way forward (a day, a step).
    const exploreDialog = async (path, depth) => {
      const dialog = dialogs().last();
      const opened = await tree(dialog);
      record(`${label(path)} -> dialog`, clip(opened, maxLines));
      await expandDropdowns(dialog, label(path));
      if (depth >= MAX_DEPTH) return;
      const options = controls(opened);
      let next = rank(options, keywords, 3, 3, true);
      // Step forward without a keyword (a day picker, a wizard step) only on a path the ticket chose.
      const onTopic = path.some((c) => matches(c, keywords).length);
      if (!next.length && onTopic) next = rank(options, [], 1, 0, true);
      for (const inner of next) {
        if (used >= budget) return;
        used += 1;
        const innerPath = [...path, inner];
        try {
          await replay(path);
          const count = await dialogs().count();
          const was = await tree(dialogs().last());
          await click(dialogs().last(), inner);
          const still = (await dialogs().count()) >= count && count > 0;
          const now = still ? await tree(dialogs().last()) : '';
          if (still && now && now !== was) await exploreDialog(innerPath, depth + 1);
          else record(`${label(innerPath)} -> page`, clip(await tree(await root()), maxLines));
        } catch (error) {
          record(label(innerPath), null, firstLine(error));
        }
      }
    };

    for (const control of rank(controls(home), keywords, budget)) {
      if (used >= budget) break;
      used += 1;
      const before = { url: page.url(), dialogs: await dialogs().count(), tree: await tree(await root()) };
      try {
        await click(page, control);
      } catch (error) {
        record(label([control]), null, firstLine(error));
        await restore();
        continue;
      }
      if ((await dialogs().count()) > before.dialogs) {
        await exploreDialog([control], 1);
      } else if (page.url() !== before.url) {
        record(`${label([control])} -> ${new URL(page.url()).pathname}`, clip(await tree(await root()), maxLines));
      } else {
        const after = await tree(await root());
        const old = new Set(before.tree.split('\n'));
        const added = after.split('\n').filter((l) => !old.has(l));
        record(`${label([control])} -> page changed`, added.length ? clip(added.join('\n'), maxLines) : '(no visible change)');
      }
      await restore();
    }
  } catch (error) {
    result.ok = false;
    result.reason = firstLine(error);
  } finally {
    await browser.close();
  }
  emit(result);
}

if (require.main === module) {
  main().catch((error) => emit({ ok: false, reason: String(error).slice(0, 400) }));
}
module.exports = { controls, rank, template, summarizeOptions };

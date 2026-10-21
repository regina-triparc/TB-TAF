/*
 * Live accessibility snapshot of the app under test, for the Automation Engineer.
 *
 *   node ui_snapshot.cjs <recipe.json> <baseURL>      (cwd = the Playwright test repository)
 *
 * Opens the app with the recipe's saved session, walks its states (click, capture,
 * close) and prints ONE JSON line: {"ok", "url", "states": [{name, ok, snapshot|error}]}.
 *
 * Read-only by construction:
 *   - every PUT / PATCH / DELETE, and every POST whose path looks like a write, is aborted;
 *   - a click whose target reads like a submit (Add service, Save changes, Delete, Send,
 *     Confirm, Book, Pay) is refused.
 * Playwright is loaded from the test repository, so no dependency is added here.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

const WRITE_POST = /(add|replace|remove|delete|create|update|book|pay|send|submit|confirm)/i;
const SUBMIT_TEXT = /^(check\s+)?(add service|save( changes)?|delete|send( quote)?|confirm|submit|book|pay)\b/i;

function emit(result) {
  process.stdout.write(`\n${JSON.stringify(result)}\n`);
}

function summarizeOptions(lines) {
  const options = lines.map((l) => /option "([^"]*)"/.exec(l)).filter(Boolean).map((m) => m[1]);
  if (!options.length) return lines.join('\n');
  const head = options.slice(0, 6).join(', ');
  const tail = options.slice(-2).join(', ');
  return `${options.length} options: ${head}${options.length > 8 ? ` ... ${tail}` : ''}`;
}

async function run(page, step) {
  const timeout = step.timeout || 30000;
  if (step.goto !== undefined) return page.goto(step.goto, { timeout });
  if (step.press) return page.keyboard.press(step.press);
  if (step.waitFor) return page.locator(step.waitFor).waitFor({ timeout });
  if (step.click) {
    const target = page.locator(step.click);
    await target.waitFor({ timeout });
    const text = ((await target.innerText().catch(() => '')) || '').replace(/\s+/g, ' ').trim();
    if (SUBMIT_TEXT.test(text)) throw new Error(`refused to click "${text}": it looks like a submit`);
    return target.click({ timeout });
  }
  throw new Error(`unknown step ${JSON.stringify(step)}`);
}

async function main() {
  const [recipePath, baseURL] = process.argv.slice(2);
  const recipe = JSON.parse(fs.readFileSync(recipePath, 'utf8'));
  const repo = process.cwd();
  const storageState = path.resolve(repo, recipe.storageState || 'e2e/.auth/state.json');
  if (!fs.existsSync(storageState)) {
    return emit({ ok: false, reason: `no saved session at ${storageState} - run npm run e2e:auth-manual` });
  }
  const { chromium } = createRequire(path.join(repo, 'package.json'))('@playwright/test');
  const browser = await chromium.launch({ headless: true });
  const result = { ok: true, url: '', states: [] };
  try {
    const context = await browser.newContext({ baseURL, storageState, viewport: { width: 1280, height: 900 } });
    await context.route('**/*', (route) => {
      const request = route.request();
      const method = request.method();
      const write = ['PUT', 'PATCH', 'DELETE'].includes(method) || (method === 'POST' && WRITE_POST.test(new URL(request.url()).pathname));
      return write ? route.abort() : route.continue();
    });
    const page = await context.newPage();
    const signedOut = () => /login|authorize|signin|\/u\/|\/landing\b/i.test(new URL(page.url()).host + new URL(page.url()).pathname);
    try {
      for (const step of recipe.open || []) await run(page, step);
    } catch (error) {
      if (signedOut()) throw new Error(`saved session expired (landed on ${new URL(page.url()).host}) - run npm run e2e:auth-manual`);
      throw new Error(`could not open the app at ${page.url()}: ${String(error.message || error).split('\n')[0]}`);
    }
    result.url = page.url();
    if (signedOut()) {
      return emit({ ok: false, reason: `saved session expired (landed on ${new URL(result.url).host}) - run npm run e2e:auth-manual` });
    }

    for (const state of recipe.states || []) {
      const entry = { name: state.name, ok: true };
      try {
        for (const step of state.actions || []) await run(page, step);
        const target = page.locator(state.capture || 'body');
        await target.waitFor({ timeout: state.timeout || 30000 });
        let lines = (await target.ariaSnapshot()).split('\n');
        if (state.summarizeOptions) {
          entry.snapshot = summarizeOptions(lines);
        } else {
          const max = state.maxLines || 80;
          if (lines.length > max) lines = [...lines.slice(0, max), `  ... (${lines.length - max} more lines)`];
          entry.snapshot = lines.join('\n');
        }
      } catch (error) {
        entry.ok = false;
        entry.error = String(error && error.message ? error.message : error).split('\n')[0].slice(0, 300);
      }
      for (const step of state.close || []) await run(page, step).catch(() => {});
      // Leave no dialog open for the next state, unless the next state continues from it.
      if (!state.keepOpen) {
        for (let i = 0; i < 3 && (await page.getByRole('dialog').count()) > 0; i += 1) await page.keyboard.press('Escape');
      }
      result.states.push(entry);
    }
  } catch (error) {
    result.ok = false;
    result.reason = String(error && error.message ? error.message : error).split('\n')[0].slice(0, 400);
  } finally {
    await browser.close();
  }
  emit(result);
}

main().catch((error) => emit({ ok: false, reason: String(error).slice(0, 400) }));

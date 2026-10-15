"""
A self-contained HTML page comparing eval result files side by side:

    python -m taf eval report evals/results/A.json evals/results/B.json [--out report.html]

Summary per run (cases passed, required / quality checks, time, tokens, cost),
pass rate per dimension, time and tokens per stage, every check with its reason,
and what each model actually produced (criteria, test plan, spec). No network:
the data is embedded and drawn with inline SVG, so the file can be opened anywhere.
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

RESULTS_DIR = Path(__file__).parent / "results"


def _run_summary(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = data.get("metadata", {})
    label = meta.get("label") or f"{data.get('provider')} ({path.stem})"
    return {"file": path.name, "label": label, "provider": data.get("provider"), "metadata": meta,
            "results": data.get("results", [])}


def build(paths: List[Path]) -> str:
    runs = [_run_summary(p) for p in paths]
    payload = json.dumps(runs).replace("</", "<\\/")
    title = " vs ".join(r["label"] for r in runs)
    return TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload).replace(
        "__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taf eval report")
    parser.add_argument("results", nargs="+", help="two or more result files (evals/results/*.json)")
    parser.add_argument("--out", default="", help="HTML file (default evals/results/report-<time>.html)")
    args = parser.parse_args(argv)
    if len(args.results) < 2:
        print("Give at least two result files to compare.")
        return 2
    if len(args.results) > 4:
        print("At most four runs per report (more cannot be told apart by colour).")
        return 2
    out = Path(args.out) if args.out else RESULTS_DIR / f"report-{datetime.now():%Y%m%d-%H%M%S}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build([Path(p) for p in args.results]), encoding="utf-8")
    print(f"Report: {out.resolve()}")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eval comparison</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --good: #0ca30c; --good-text: #006300; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --good-text: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --good-text: #0ca30c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 12px; }
h3 { font-size: 14px; margin: 16px 0 8px; }
.sub { color: var(--ink-2); margin: 0 0 16px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 16px; min-width: 0; overflow-x: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin: 8px 0 0; }
.chip { display: inline-flex; align-items: center; gap: 8px; color: var(--ink); }
.sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; flex: none; }
.models { color: var(--ink-2); font-size: 12px; }
.tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr)); }
.tile h3 { margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 16px; }
.kv dt { color: var(--ink-2); }
.kv dd { margin: 0; font-variant-numeric: tabular-nums; text-align: right; }
.big { font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
.charts { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr)); }
.chart-title { font-weight: 600; margin: 0 0 2px; }
.chart-note { color: var(--ink-2); font-size: 12px; margin: 0 0 8px; }
svg text { fill: var(--muted); font-size: 11px; }
svg .val { fill: var(--ink-2); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .cat { fill: var(--ink-2); font-size: 12px; }
.tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
       border: 1px solid var(--ring); border-radius: 8px; padding: 6px 10px; font-size: 12px;
       box-shadow: 0 4px 16px rgba(0,0,0,.12); display: none; z-index: 10; max-width: 320px; }
details > summary { cursor: pointer; color: var(--ink-2); margin: 8px 0 0; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.st { white-space: nowrap; }
.ok { color: var(--good-text); }
.no { color: var(--critical); }
.soft { color: var(--muted); font-size: 11px; margin-left: 4px; }
.why { color: var(--ink-2); font-size: 12px; }
.cols { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); }
.col-h { display: flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 8px; }
pre { background: var(--page); border: 1px solid var(--ring); border-radius: 8px; padding: 10px;
      overflow: auto; max-height: 520px; font-size: 12px; }
ol, ul { margin: 4px 0; padding-left: 20px; }
.empty { color: var(--muted); font-style: italic; }
.toolbar { display: flex; justify-content: space-between; align-items: start; gap: 12px; flex-wrap: wrap; }
button.theme { background: var(--surface); color: var(--ink); border: 1px solid var(--ring);
               border-radius: 8px; padding: 6px 10px; cursor: pointer; }
@media (max-width: 600px) { .big { font-size: 22px; } }
</style>
</head>
<body>
<main>
  <div class="toolbar">
    <div>
      <h1>Eval comparison</h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <button class="theme" id="theme" type="button">Toggle theme</button>
  </div>
  <div class="legend" id="legend"></div>

  <h2>Summary</h2>
  <div class="tiles" id="tiles"></div>

  <h2>Where they differ</h2>
  <div class="charts">
    <div class="card"><p class="chart-title">Checks passed per dimension</p>
      <p class="chart-note">Share of checks passed, all cases. Hover a bar for the counts.</p>
      <div id="chart-dims"></div><details><summary>Show as table</summary><div id="table-dims"></div></details></div>
    <div class="card"><p class="chart-title">Time per stage</p>
      <p class="chart-note">Seconds, all cases summed.</p>
      <div id="chart-time"></div><details><summary>Show as table</summary><div id="table-time"></div></details></div>
    <div class="card"><p class="chart-title">Tokens per stage</p>
      <p class="chart-note">Model input + output tokens (prompt-cache reads excluded).</p>
      <div id="chart-tokens"></div><details><summary>Show as table</summary><div id="table-tokens"></div></details></div>
  </div>

  <div id="cases"></div>
  <p class="sub" style="margin-top:32px">Generated __GENERATED__ by <code>python -m taf eval report</code>.
    A passing check or test run is not proof that the tests cover the requirements - read the outputs.</p>
</main>
<div class="tip" id="tip" role="status"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  const runs = JSON.parse(document.getElementById("data").textContent);
  const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];
  const STAGES = ["triage", "architect", "automation"];
  const NS = "http://www.w3.org/2000/svg";
  const tip = document.getElementById("tip");

  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function svg(tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function swatch(i) { const s = el("span", { class: "sw" }); s.style.background = SERIES[i]; return s; }
  function fmt(n) { return Number(n || 0).toLocaleString(); }
  function frac(s) { const [a, b] = String(s || "0/0").split("/").map(Number); return [a || 0, b || 0]; }
  function showTip(ev, text) {
    tip.textContent = text; tip.style.display = "block";
    const x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + "px"; tip.style.top = (ev.clientY + 14) + "px";
  }
  function hideTip() { tip.style.display = "none"; }

  // ---- header ---------------------------------------------------------------
  const caseIds = [...new Set(runs.flatMap(r => r.results.map(c => c.id)))];
  document.getElementById("subtitle").textContent =
    `${caseIds.length} case(s): ${caseIds.join(", ")} · dataset ${runs.map(r => r.metadata.dataset_version || "?").join(" / ")}`;
  const legend = document.getElementById("legend");
  runs.forEach((r, i) => {
    const chip = el("div", { class: "chip" });
    chip.append(swatch(i));
    const txt = el("div");
    txt.append(el("div", {}, r.label));
    const models = Object.entries(r.metadata.models || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
    txt.append(el("div", { class: "models" }, models || r.provider));
    chip.append(txt);
    legend.append(chip);
  });

  // ---- summary tiles --------------------------------------------------------
  function totals(run) {
    const t = { passed: 0, cases: run.results.length, hard: [0, 0], soft: [0, 0], secs: 0, tokens: 0, cost: 0,
                byStage: {}, dims: {} };
    for (const c of run.results) {
      if (c.passed) t.passed++;
      t.cost += c.cloud_cost_usd || 0;
      for (const [stage, s] of Object.entries(c.stages || {})) {
        const h = frac(s.hard), so = frac(s.soft);
        t.hard[0] += h[0]; t.hard[1] += h[1]; t.soft[0] += so[0]; t.soft[1] += so[1];
        t.secs += s.seconds || 0; t.tokens += s.tokens || 0;
        const b = t.byStage[stage] = t.byStage[stage] || { secs: 0, tokens: 0 };
        b.secs += s.seconds || 0; b.tokens += s.tokens || 0;
        for (const ch of s.checks || []) {
          const d = t.dims[ch.dimension || "other"] = t.dims[ch.dimension || "other"] || [0, 0];
          d[0] += ch.passed ? 1 : 0; d[1] += 1;
        }
      }
    }
    return t;
  }
  const T = runs.map(totals);
  const tiles = document.getElementById("tiles");
  runs.forEach((r, i) => {
    const t = T[i], card = el("div", { class: "card tile" });
    const h = el("h3"); h.append(swatch(i), document.createTextNode(r.label)); card.append(h);
    card.append(el("div", { class: "big" }, `${t.passed}/${t.cases} cases passed`));
    const dl = el("dl", { class: "kv" });
    const rows = [
      ["Required checks", `${t.hard[0]}/${t.hard[1]}`],
      ["Quality checks", `${t.soft[0]}/${t.soft[1]}`],
      ["Time", `${Math.round(t.secs)} s`],
      ["Tokens", fmt(t.tokens)],
      ["Cost", `$${t.cost.toFixed(4)}`],
    ];
    for (const [k, v] of rows) { dl.append(el("dt", {}, k), el("dd", {}, v)); }
    card.append(dl);
    tiles.append(card);
  });

  // ---- grouped horizontal bars ------------------------------------------------
  function barChart(target, categories, valueOf, labelOf, tipOf, unitMax) {
    const W = 520, rowH = 10 * runs.length + 6 * (runs.length - 1) + 16, left = 110, right = 70, top = 6;
    const H = top + categories.length * rowH + 4;
    const max = unitMax || Math.max(1, ...categories.flatMap(c => runs.map((_, i) => valueOf(c, i))));
    const plotW = W - left - right;
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img",
                              "aria-label": "bar chart; see the table view" });
    root.append(svg("line", { x1: left, x2: left, y1: top - 2, y2: H - 2, stroke: "var(--axis)", "stroke-width": 1 }));
    categories.forEach((cat, ci) => {
      const y0 = top + ci * rowH;
      root.append(svg("text", { x: left - 8, y: y0 + rowH / 2, "text-anchor": "end", "dominant-baseline": "middle",
                                class: "cat" }, cat));
      runs.forEach((_, i) => {
        const v = valueOf(cat, i), w = Math.max(v > 0 ? 2 : 0, (v / max) * plotW);
        const y = y0 + 8 + i * 16 - (runs.length > 2 ? 4 : 0);
        const bar = svg("path", { d: roundedBar(left, y, w, 10), fill: SERIES[i] });
        const hit = svg("rect", { x: left, y: y - 3, width: plotW, height: 16, fill: "transparent" });
        hit.addEventListener("mousemove", ev => showTip(ev, `${runs[i].label} · ${cat}: ${tipOf(cat, i)}`));
        hit.addEventListener("mouseleave", hideTip);
        root.append(bar);
        root.append(svg("text", { x: left + w + 6, y: y + 5, "dominant-baseline": "middle", class: "val" },
                        labelOf(cat, i)));
        root.append(hit);
      });
    });
    document.getElementById(target).append(root);
  }
  function roundedBar(x, y, w, h) {   // 4px rounded data-end, square at the baseline
    if (w <= 0) return "";
    const r = Math.min(4, w, h / 2);
    return `M${x},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - r},${y + h} H${x} Z`;
  }
  function table(target, head, rows) {
    const t = el("table"), tr = el("tr");
    head.forEach(h => tr.append(el("th", {}, h)));
    t.append(tr);
    rows.forEach(r => { const row = el("tr"); r.forEach((c, i) => row.append(el("td", i ? { class: "num" } : {}, c))); t.append(row); });
    document.getElementById(target).append(t);
  }

  const dims = [...new Set(T.flatMap(t => Object.keys(t.dims)))].sort();
  const pct = (d, i) => { const v = T[i].dims[d]; return v && v[1] ? 100 * v[0] / v[1] : 0; };
  barChart("chart-dims", dims, pct, (d, i) => T[i].dims[d] ? `${Math.round(pct(d, i))}%` : "-",
           (d, i) => T[i].dims[d] ? `${T[i].dims[d][0]} of ${T[i].dims[d][1]} checks` : "no checks", 100);
  table("table-dims", ["Dimension", ...runs.map(r => r.label)],
        dims.map(d => [d, ...runs.map((_, i) => T[i].dims[d] ? `${T[i].dims[d][0]}/${T[i].dims[d][1]}` : "-")]));

  const stages = STAGES.filter(s => T.some(t => t.byStage[s]));
  const secs = (s, i) => (T[i].byStage[s] || {}).secs || 0;
  const toks = (s, i) => (T[i].byStage[s] || {}).tokens || 0;
  barChart("chart-time", stages, secs, (s, i) => `${Math.round(secs(s, i))} s`, (s, i) => `${secs(s, i).toFixed(1)} s`);
  table("table-time", ["Stage", ...runs.map(r => r.label)], stages.map(s => [s, ...runs.map((_, i) => `${secs(s, i).toFixed(1)} s`)]));
  barChart("chart-tokens", stages, toks, (s, i) => fmt(toks(s, i)), (s, i) => `${fmt(toks(s, i))} tokens`);
  table("table-tokens", ["Stage", ...runs.map(r => r.label)], stages.map(s => [s, ...runs.map((_, i) => fmt(toks(s, i)))]));

  // ---- per case: checks and outputs -------------------------------------------
  const casesEl = document.getElementById("cases");
  for (const id of caseIds) {
    const perRun = runs.map(r => r.results.find(c => c.id === id));
    casesEl.append(el("h2", {}, `Case ${id}`));

    // checks, row per (stage, check), column per run
    const card = el("div", { class: "card" });
    card.append(el("p", { class: "chart-title" }, "Checks"));
    card.append(el("p", { class: "chart-note" }, "✓ passed · ✗ failed. Quality checks are marked; they never fail a case."));
    const t = el("table"), head = el("tr");
    head.append(el("th", {}, "Stage"), el("th", {}, "Check"));
    runs.forEach((r, i) => { const th = el("th"); th.append(swatch(i), document.createTextNode(" " + r.label)); head.append(th); });
    t.append(head);
    for (const stage of STAGES) {
      const names = [];
      perRun.forEach(c => ((c && c.stages[stage] && c.stages[stage].checks) || []).forEach(ch => {
        if (!names.includes(ch.name)) names.push(ch.name);
      }));
      for (const name of names) {
        const row = el("tr");
        row.append(el("td", {}, stage));
        const nameCell = el("td", {}, name);
        const anyCheck = perRun.map(c => c && c.stages[stage] && c.stages[stage].checks.find(ch => ch.name === name)).find(Boolean);
        if (anyCheck && !anyCheck.hard) nameCell.append(el("span", { class: "soft" }, "quality"));
        row.append(nameCell);
        perRun.forEach(c => {
          const ch = c && c.stages[stage] && c.stages[stage].checks.find(x => x.name === name);
          const td = el("td");
          if (!ch) { td.append(el("span", { class: "empty" }, c && c.stages[stage] ? "-" : "stage not reached")); }
          else {
            td.append(el("span", { class: "st " + (ch.passed ? "ok" : "no") }, ch.passed ? "✓ passed" : "✗ failed"));
            if (ch.detail) td.append(el("div", { class: "why" }, ch.detail.length > 220 ? ch.detail.slice(0, 220) + "…" : ch.detail));
          }
          row.append(td);
        });
        t.append(row);
      }
    }
    card.append(t);
    casesEl.append(card);

    // outputs side by side
    casesEl.append(el("h3", {}, "What each model produced"));
    const cols = el("div", { class: "cols" });
    perRun.forEach((c, i) => {
      const col = el("div", { class: "card" });
      const h = el("div", { class: "col-h" }); h.append(swatch(i), document.createTextNode(runs[i].label)); col.append(h);
      const out = (c && c.outputs) || null;
      if (!out) { col.append(el("p", { class: "empty" }, "This result file has no saved outputs (run it again with the current eval runner).")); cols.append(col); return; }
      const tr = out.triage || {};
      col.append(el("p", {}, `Risk: ${tr.risk_level || "-"} · ${(tr.acceptance_criteria || []).length} criteria · ${(out.test_cases || []).length} test cases · spec ${fmt((out.spec || "").length)} chars`));
      const crit = el("details"); crit.append(el("summary", {}, `Acceptance criteria (${(tr.acceptance_criteria || []).length})`));
      const ol = el("ol"); (tr.acceptance_criteria || []).forEach(a => ol.append(el("li", {}, a))); crit.append(ol); col.append(crit);
      const q = el("details"); q.append(el("summary", {}, `Open questions (${(tr.gaps_and_questions || []).length})`));
      const ul = el("ul"); (tr.gaps_and_questions || []).forEach(a => ul.append(el("li", {}, a))); q.append(ul); col.append(q);
      const plan = el("details"); plan.append(el("summary", {}, `Test plan (${(out.test_cases || []).length} cases)`));
      const pt = el("table"), ph = el("tr"); ["ID", "Test", "Priority", "Refs"].forEach(x => ph.append(el("th", {}, x))); pt.append(ph);
      (out.test_cases || []).forEach(tc => {
        const r = el("tr");
        const sumCell = el("td", {}, tc.summary || "");
        const steps = el("div", { class: "why" }, (tc.steps || []).map(s => `${s.action} → ${s.expected_result}`).join(" · "));
        sumCell.append(steps);
        r.append(el("td", {}, tc.test_id || ""), sumCell, el("td", {}, tc.priority || ""),
                 el("td", {}, (tc.acceptance_criteria_refs || []).join(", ")));
        pt.append(r);
      });
      plan.append(pt); col.append(plan);
      const spec = el("details"); spec.append(el("summary", {}, "Generated spec"));
      spec.append(out.spec ? el("pre", {}, out.spec) : el("p", { class: "empty" }, "No usable spec was produced."));
      col.append(spec);
      if ((out.errors || []).length) { col.append(el("p", { class: "no" }, out.errors.join("; "))); }
      cols.append(col);
    });
    casesEl.append(cols);
  }

  document.getElementById("theme").addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  });
})();
</script>
</body>
</html>
"""

"""
Per-run usage report: tokens, cost, model and wall time for every stage of a ticket's run.

Every model call is booked against the stage the run is in (the PhaseRecorder in
taf/runs/stages.py says which), and every stage's duration is booked when it closes.
When a run stops - completed, failed, cancelled or paused at approval - the report is
written to

    <REPORTS_DIR>/usage/<TICKET>-<run8>.json   the full record
    <REPORTS_DIR>/usage/<TICKET>-<run8>.md     the same as tables
    <REPORTS_DIR>/usage/usage.csv              one row per (run, stage, agent, model), all runs

A run resumed after a pause (or a restart) picks up its earlier stages from the JSON,
so the final report covers the whole run.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from taf.config import SETTINGS, project_path
from taf.llm.pricing import TokenUsage

log = logging.getLogger("qa.usage")

STAGE_ORDER = ("context", "triage", "architect", "automation", "approval", "publish", "report", "closeout")
CSV_COLUMNS = ["run_id", "ticket_id", "run_state", "finished_at", "stage", "stage_state", "duration_s",
               "agent", "provider", "model", "calls", "input_tokens", "output_tokens",
               "cache_read_tokens", "cache_write_tokens", "total_tokens", "cost_usd"]
UNATTRIBUTED = "unattributed"  # a model call made outside any recorded stage


def _empty_tokens() -> Dict[str, Any]:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}


def _add(into: Dict[str, Any], other: Dict[str, Any]) -> None:
    for key in _empty_tokens():
        into[key] = into.get(key, 0) + other.get(key, 0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def usage_dir() -> Path:
    return project_path(SETTINGS.reports_dir) / "usage"


def report_stem(ticket_id: str, run_id: str) -> str:
    return f"{ticket_id}-{run_id[:8]}"


class UsageLedger:
    """Thread-safe per-run ledger. Model calls arrive on node threads, stage events on the driver."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._stage: Dict[str, str] = {}

    # ----------------------------------------------------------------- write --
    def begin(self, run_id: str, ticket_id: str, provider: str = "",
              routes: Optional[Dict[str, Any]] = None) -> None:
        """Open (or reopen, after a pause or restart) the ledger for a run."""
        with self._lock:
            if run_id in self._runs:
                return
            record = self._load(ticket_id, run_id) or {
                "run_id": run_id, "ticket_id": ticket_id, "started_at": _now(), "stages": {}}
            if provider:
                record["provider"] = provider
            if routes:
                record["routes"] = routes
            self._runs[run_id] = record

    def set_stage(self, run_id: str, stage: Optional[str]) -> None:
        with self._lock:
            if stage:
                self._stage[run_id] = stage
            else:
                self._stage.pop(run_id, None)

    def record_call(self, run_id: str, agent: str, provider: str, model: str,
                    usage: TokenUsage, cost_cents: float = 0.0) -> None:
        """Book one model call against the run's current stage."""
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:  # a call outside a recorded run (evals, ad-hoc agent calls)
                return
            stage = self._stage_entry(record, self._stage.get(run_id, UNATTRIBUTED))
            key = f"{agent}|{provider}|{model}"
            row = stage["models"].setdefault(key, {"agent": agent, "provider": provider, "model": model,
                                                   **_empty_tokens()})
            _add(row, {"calls": 1, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                       "cache_read_tokens": usage.cache_read_tokens,
                       "cache_write_tokens": usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
                       "total_tokens": usage.input_tokens + usage.output_tokens,
                       "cost_usd": cost_cents / 100})

    def stage_finished(self, run_id: str, stage: str, duration_s: float, state: str) -> None:
        """Add a stage's wall time (a stage paused at approval and resumed is booked twice, summed)."""
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            entry = self._stage_entry(record, stage)
            entry["duration_s"] = round(entry.get("duration_s", 0.0) + duration_s, 1)
            entry["state"] = state

    def _stage_entry(self, record: Dict[str, Any], stage: str) -> Dict[str, Any]:
        return record["stages"].setdefault(stage, {"state": "", "duration_s": 0.0, "models": {}})

    # ---------------------------------------------------------------- report --
    def snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The run's report as a JSON-friendly dict, with per-stage and run totals."""
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            record = json.loads(json.dumps(record))  # a deep copy the caller may keep
        stages: List[Dict[str, Any]] = []
        run_totals = _empty_tokens()
        names = sorted(record["stages"], key=lambda s: (STAGE_ORDER.index(s) if s in STAGE_ORDER
                                                        else len(STAGE_ORDER), s))
        for name in names:
            entry = record["stages"][name]
            models = list(entry["models"].values())
            totals = _empty_tokens()
            for row in models:
                row["cost_usd"] = round(row["cost_usd"], 6)
                _add(totals, row)
            totals["cost_usd"] = round(totals["cost_usd"], 6)
            _add(run_totals, totals)
            stages.append({"stage": name, "state": entry.get("state", ""),
                           "duration_s": entry.get("duration_s", 0.0),
                           "models": models, "totals": totals})
        run_totals["cost_usd"] = round(run_totals["cost_usd"], 6)
        return {
            "run_id": record["run_id"], "ticket_id": record["ticket_id"],
            "provider": record.get("provider", ""), "routes": record.get("routes", {}),
            "started_at": record.get("started_at", ""), "finished_at": record.get("finished_at", ""),
            "run_state": record.get("run_state", ""),
            "duration_s": round(sum(s["duration_s"] for s in stages), 1),
            "stages": stages, "totals": run_totals,
        }

    def write_report(self, run_id: str, run_state: str, release: bool = True) -> Optional[str]:
        """
        Write the JSON + Markdown report and the CSV rows; returns the Markdown path.
        `release=False` keeps the run open (paused at approval). Never raises: a
        report that cannot be written must not fail the run.
        """
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            record["run_state"], record["finished_at"] = run_state, _now()
            raw = json.loads(json.dumps(record))
        report = self.snapshot(run_id)
        try:
            out = usage_dir()
            out.mkdir(parents=True, exist_ok=True)
            stem = report_stem(report["ticket_id"], run_id)
            (out / f"{stem}.json").write_text(json.dumps({**report, "_ledger": raw}, indent=2), encoding="utf-8")
            md_path = out / f"{stem}.md"
            md_path.write_text(render_markdown(report), encoding="utf-8")
            if release:
                _append_csv(out / "usage.csv", report)
            return str(md_path)
        except OSError as exc:
            log.warning("[%s] Could not write the usage report: %s", report["ticket_id"], exc)
            return None
        finally:
            if release:
                with self._lock:
                    self._runs.pop(run_id, None)
                    self._stage.pop(run_id, None)

    def _load(self, ticket_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        path = usage_dir() / f"{report_stem(ticket_id, run_id)}.json"
        try:
            ledger = json.loads(path.read_text(encoding="utf-8")).get("_ledger")
        except (OSError, ValueError):
            return None
        return ledger if isinstance(ledger, dict) and ledger.get("run_id") == run_id else None


def _fmt_duration(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"


def render_markdown(report: Dict[str, Any]) -> str:
    t = report["totals"]
    lines = [
        f"# Usage report - {report['ticket_id']}",
        "",
        f"- **Run:** `{report['run_id']}` ({report['run_state'] or 'running'})",
        f"- **Provider:** {report['provider'] or '-'}",
        f"- **Started:** {report['started_at']}  **Finished:** {report['finished_at'] or '-'}",
        f"- **Time in stages:** {_fmt_duration(report['duration_s'])}",
        f"- **Tokens:** {t['total_tokens']:,} ({t['input_tokens']:,} in / {t['output_tokens']:,} out, "
        f"{t['cache_read_tokens']:,} cache read) in {t['calls']} call(s)",
        f"- **Cost:** ${t['cost_usd']:.4f}",
        "",
        "## Per stage",
        "",
        "| Stage | State | Time | Model(s) | Calls | Input | Output | Cache read | Total tokens | Cost (USD) |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in report["stages"]:
        st = s["totals"]
        models = ", ".join(sorted({f"`{m['model']}`" for m in s["models"]})) or "-"
        lines.append(f"| {s['stage']} | {s['state'] or '-'} | {_fmt_duration(s['duration_s'])} | {models} | "
                     f"{st['calls']} | {st['input_tokens']:,} | {st['output_tokens']:,} | "
                     f"{st['cache_read_tokens']:,} | {st['total_tokens']:,} | {st['cost_usd']:.4f} |")
    lines.append(f"| **total** | | **{_fmt_duration(report['duration_s'])}** | | **{t['calls']}** | "
                 f"**{t['input_tokens']:,}** | **{t['output_tokens']:,}** | **{t['cache_read_tokens']:,}** | "
                 f"**{t['total_tokens']:,}** | **{t['cost_usd']:.4f}** |")
    detail = [(s["stage"], m) for s in report["stages"] for m in s["models"]]
    if detail:
        lines += ["", "## Per agent and model", "",
                  "| Stage | Agent | Provider | Model | Calls | Input | Output | Cache read | Cache write | Cost (USD) |",
                  "|---|---|---|---|---:|---:|---:|---:|---:|---:|"]
        for stage, m in detail:
            lines.append(f"| {stage} | {m['agent']} | {m['provider']} | `{m['model']}` | {m['calls']} | "
                         f"{m['input_tokens']:,} | {m['output_tokens']:,} | {m['cache_read_tokens']:,} | "
                         f"{m['cache_write_tokens']:,} | {m['cost_usd']:.4f} |")
    if report.get("routes"):
        lines += ["", "## Configured routes", "", "| Agent | Provider | Model | Effort |", "|---|---|---|---|"]
        for key, r in report["routes"].items():
            lines.append(f"| {key} | {r.get('provider', '')} | `{r.get('model', '')}` | {r.get('effort', '')} |")
    return "\n".join(lines) + "\n"


def _append_csv(path: Path, report: Dict[str, Any]) -> None:
    rows = []
    for s in report["stages"]:
        base = {"run_id": report["run_id"], "ticket_id": report["ticket_id"], "run_state": report["run_state"],
                "finished_at": report["finished_at"], "stage": s["stage"], "stage_state": s["state"],
                "duration_s": s["duration_s"]}
        if not s["models"]:
            rows.append({**base, **_empty_tokens(), "agent": "", "provider": "", "model": ""})
        for m in s["models"]:
            rows.append({**base, **m})
    # A resumed run (cancelled or failed, then continued) replaces its earlier rows.
    kept: List[Dict[str, Any]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            kept = [r for r in csv.DictReader(fh) if r.get("run_id") != report["run_id"]]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept + rows)


USAGE = UsageLedger()

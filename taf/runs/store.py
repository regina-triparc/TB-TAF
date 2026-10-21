"""
SQLite persistence for QA runs and their pipeline phases.

The in-memory `RunRegistry` in main.py forgets everything on restart, which makes
a dashboard useless after the first crash. This module is the durable record: one
row per run, one row per phase transition. It is deliberately dependency-free
(sqlite3 is in the standard library) and safe to call from the worker threads that
execute pipeline stages.

Phase vocabulary (what the board renders):
    triage -> architect -> automation -> report -> closeout
    (plus approval -> publish between automation and report)
Phase states: running | waiting | ok | failed | cancelled
Run states:   running | waiting | completed | failed | cancelled
`waiting` = paused at the approval gate; the LangGraph checkpoint (a separate file,
QA_CHECKPOINT_DB_PATH) holds the run until it is resumed. These are values in the
existing TEXT columns - the schema is unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taf.config import SETTINGS, project_path

PHASES = ("triage", "architect", "automation", "approval", "publish", "report", "closeout")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    ticket_id   TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    stage       TEXT NOT NULL DEFAULT 'full',
    state       TEXT NOT NULL,
    outcome     TEXT,
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_ticket ON runs(ticket_id, started_at DESC);

CREATE TABLE IF NOT EXISTS phases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    ticket_id   TEXT NOT NULL,
    phase       TEXT NOT NULL,
    state       TEXT NOT NULL,
    detail      TEXT,
    artifact    TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_phases_run ON phases(run_id, id);
"""


# A generated spec or a Playwright log can be large; keep one phase's record bounded
# so a runaway run cannot bloat the database the dashboard reads on every poll.
MAX_ARTIFACT_CHARS = 60_000


def _with_artifact(row: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the stored JSON blob back into the row, dropping it if unreadable."""
    raw = row.pop("artifact", None)
    if raw:
        try:
            row["artifact"] = json.loads(raw)
        except ValueError:
            row["artifact"] = None
    else:
        row["artifact"] = None
    return row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStore:
    """Thread-safe SQLite store. One connection, serialised by a lock."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False: pipeline stages run on worker threads, and every
        # access below is already serialised by self._lock.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Additive column adds, so a database written by an older build still opens."""
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(phases)")}
        if "artifact" not in have:
            self._db.execute("ALTER TABLE phases ADD COLUMN artifact TEXT")

    # ----------------------------------------------------------------- write --
    def start_run(self, run_id: str, ticket_id: str, summary: str, stage: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO runs (run_id, ticket_id, summary, stage, state, started_at)"
                " VALUES (?,?,?,?,'running',?)",
                (run_id, ticket_id, summary, stage, _now()),
            )
            self._db.commit()

    def finish_run(self, run_id: str, state: str, outcome: str = "", error: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE runs SET state=?, outcome=?, error=?, finished_at=? WHERE run_id=?",
                (state, outcome or None, error or None, _now(), run_id),
            )
            self._db.commit()

    def phase_started(self, run_id: str, ticket_id: str, phase: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO phases (run_id, ticket_id, phase, state, started_at)"
                " VALUES (?,?,?,'running',?)",
                (run_id, ticket_id, phase, _now()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def phase_finished(self, phase_row_id: int, state: str, detail: str = "",
                       artifact: Optional[Dict[str, Any]] = None) -> None:
        blob = None
        if artifact is not None:
            try:
                blob = json.dumps(artifact, default=str)[:MAX_ARTIFACT_CHARS]
            except (TypeError, ValueError):
                blob = None
        with self._lock:
            self._db.execute(
                "UPDATE phases SET state=?, detail=?, artifact=?, finished_at=? WHERE id=?",
                (state, detail or None, blob, _now(), phase_row_id),
            )
            self._db.commit()

    def set_run_state(self, run_id: str, state: str) -> None:
        """running / waiting, without finishing the run (a pause, or a resume)."""
        with self._lock:
            self._db.execute("UPDATE runs SET state=?, error=NULL, finished_at=NULL WHERE run_id=?", (state, run_id))
            self._db.commit()

    def phase_update(self, phase_row_id: int, state: str, detail: str = "",
                     artifact: Optional[Dict[str, Any]] = None) -> None:
        """Change a phase's state and detail but leave it open (a run paused at approval)."""
        blob = json.dumps(artifact, default=str)[:MAX_ARTIFACT_CHARS] if artifact is not None else None
        with self._lock:
            self._db.execute("UPDATE phases SET state=?, detail=?, artifact=? WHERE id=?",
                             (state, detail or None, blob, phase_row_id))
            self._db.commit()

    def open_phase(self, run_id: str) -> Optional[Tuple[int, str]]:
        """(row id, phase) of the run's phase that is still waiting, if any."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, phase FROM phases WHERE run_id=? AND state='waiting' ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return (int(row["id"]), row["phase"]) if row else None

    def run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def mark_stale_running_as_interrupted(self) -> int:
        """
        Any run still 'running' at startup died with the previous process. Runs that are
        'waiting' for approval are kept: their checkpoint survives restarts.
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE runs SET state='failed', error='interrupted by service restart',"
                " finished_at=? WHERE state='running'",
                (_now(),),
            )
            self._db.execute(
                "UPDATE phases SET state='failed', detail='interrupted by service restart',"
                " finished_at=? WHERE state='running'",
                (_now(),),
            )
            self._db.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ read --
    def recent_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            out = []
            for r in rows:
                phases = self._db.execute(
                    "SELECT phase, state, detail, artifact, started_at, finished_at FROM phases"
                    " WHERE run_id=? ORDER BY id",
                    (r["run_id"],),
                ).fetchall()
                run = dict(r)
                run["phases"] = [_with_artifact(dict(p)) for p in phases]
                out.append(run)
            return out

    def latest_by_ticket(self) -> Dict[str, Dict[str, Any]]:
        """Most recent run per ticket, keyed by ticket id - what the board joins on."""
        latest: Dict[str, Dict[str, Any]] = {}
        for run in self.recent_runs(limit=300):
            latest.setdefault(run["ticket_id"], run)
        return latest

    def active_run_for(self, ticket_id: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id FROM runs WHERE ticket_id=? AND state IN ('running', 'waiting') LIMIT 1",
                (ticket_id,),
            ).fetchone()
            return row["run_id"] if row else None


_STORE: Optional[RunStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> RunStore:
    """Process-wide singleton, created on first use."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = RunStore(str(project_path(SETTINGS.qa_db_path)))
        return _STORE

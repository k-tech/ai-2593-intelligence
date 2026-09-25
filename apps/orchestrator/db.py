from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from .domain import SourceEvent, canonical_json, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, trigger_type TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, snapshot_id TEXT, error TEXT,
  manifest_path TEXT
);
CREATE TABLE IF NOT EXISTS run_steps (
  run_id TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, attempts INTEGER NOT NULL DEFAULT 1,
  error TEXT, PRIMARY KEY(run_id, step), FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS source_events (
  event_id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
  revision INTEGER NOT NULL, event_time TEXT NOT NULL, ingested_at TEXT NOT NULL,
  content_sha256 TEXT NOT NULL, sensitivity TEXT NOT NULL, body_json TEXT NOT NULL,
  UNIQUE(source, external_id, content_sha256), UNIQUE(source, external_id, revision)
);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, manifest_path TEXT NOT NULL,
  input_path TEXT NOT NULL, event_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  status TEXT NOT NULL, duration_ms INTEGER NOT NULL, exit_code INTEGER NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
  output_path TEXT, stderr_path TEXT, validation_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  UNIQUE(run_id, provider), FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, provider TEXT NOT NULL,
  reviewer TEXT NOT NULL, scores_json TEXT NOT NULL, accepted INTEGER NOT NULL,
  notes TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_reviews (
  risk_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, provider TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('supported','rejected')),
  evidence_event_id TEXT, reviewer TEXT NOT NULL, notes TEXT, reviewed_at TEXT NOT NULL,
  CHECK((status='supported' AND evidence_event_id IS NOT NULL) OR
        (status='rejected' AND evidence_event_id IS NULL)),
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id),
  FOREIGN KEY(evidence_event_id) REFERENCES source_events(event_id)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            if "manifest_path" not in columns:
                conn.execute("ALTER TABLE runs ADD COLUMN manifest_path TEXT")
            model_columns = {row[1] for row in conn.execute("PRAGMA table_info(model_runs)")}
            if "attempts" not in model_columns:
                conn.execute("ALTER TABLE model_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
        self.path.chmod(0o600)

    def create_run(self, run_id: str, trigger_type: str) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO runs
              (run_id,trigger_type,status,started_at,finished_at,snapshot_id,error,manifest_path)
              VALUES (?,?,'running',?,NULL,NULL,NULL,NULL)""",
              (run_id, trigger_type, utc_now()))

    def finish_run(self, run_id: str, status: str, snapshot_id: str | None = None,
                   error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE runs SET status=?, finished_at=?, snapshot_id=?, error=? WHERE run_id=?",
                         (status, utc_now(), snapshot_id, error, run_id))

    def set_run_manifest(self, run_id: str, manifest_path: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE runs SET manifest_path=? WHERE run_id=?", (manifest_path, run_id))

    def step(self, run_id: str, name: str, status: str, error: str | None = None) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("""INSERT INTO run_steps(run_id,step,status,started_at,finished_at,error)
              VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,step) DO UPDATE SET
              status=excluded.status,
              started_at=CASE WHEN excluded.status='running' THEN excluded.started_at
                              ELSE run_steps.started_at END,
              finished_at=excluded.finished_at, error=excluded.error,
              attempts=CASE WHEN excluded.status='running' THEN run_steps.attempts+1
                            ELSE run_steps.attempts END""",
                         (run_id, name, status, now, now if status != "running" else None, error))

    def latest_run_for_trigger(self, trigger_type: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE trigger_type=? ORDER BY started_at DESC LIMIT 1",
                (trigger_type,),
            ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def upsert_event(self, event: SourceEvent) -> bool:
        with self.connect() as conn:
            result = conn.execute("""INSERT OR IGNORE INTO source_events
              (event_id,source,external_id,revision,event_time,ingested_at,content_sha256,sensitivity,body_json)
              VALUES(?,?,?,?,?,?,?,?,?)""", (event.event_id, event.source, event.external_id,
              event.revision, event.event_time, event.ingested_at, event.content_sha256,
              event.sensitivity, canonical_json(event.to_dict())))
            return result.rowcount == 1

    def next_revision(self, source: str, external_id: str, content_hash: str) -> tuple[int, bool]:
        with self.connect() as conn:
            same = conn.execute("SELECT revision FROM source_events WHERE source=? AND external_id=? AND content_sha256=?",
                                (source, external_id, content_hash)).fetchone()
            if same:
                return int(same[0]), False
            row = conn.execute("SELECT MAX(revision) FROM source_events WHERE source=? AND external_id=?",
                               (source, external_id)).fetchone()
            return int(row[0] or 0) + 1, True

    def events(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("""SELECT e.body_json FROM source_events e
              JOIN (
                SELECT source, external_id, MAX(revision) AS revision
                FROM source_events GROUP BY source, external_id
              ) latest ON latest.source=e.source AND latest.external_id=e.external_id
                      AND latest.revision=e.revision
              ORDER BY e.event_time,e.event_id""").fetchall()
        import json
        return [json.loads(row[0]) for row in rows]

    def events_between(self, start_utc: str, end_utc: str) -> list[dict]:
        """Return latest revisions whose event time is in [start_utc, end_utc)."""
        return [event for event in self.events()
                if start_utc <= event["event_time"] < end_utc]

    def latest_events_for(self, keys: set[tuple[str, str]]) -> list[dict]:
        return [event for event in self.events()
                if (event["source"], event["external_id"]) in keys]

    def events_by_ids(self, event_ids: Iterable[str]) -> list[dict]:
        ids = list(event_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT body_json FROM source_events WHERE event_id IN ({placeholders})", ids
            ).fetchall()
        import json
        events_by_id = {event["event_id"]: event for event in (json.loads(row[0]) for row in rows)}
        return [events_by_id[event_id] for event_id in ids if event_id in events_by_id]

    def artifact(self, artifact_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def refresh_events(self, events: list[SourceEvent]) -> None:
        """Atomically replace normalized bodies without changing event identity/history."""
        with self.connect() as conn:
            for event in events:
                result = conn.execute("""UPDATE source_events SET event_time=?, ingested_at=?,
                  content_sha256=?, sensitivity=?, body_json=? WHERE event_id=?""",
                  (event.event_time, event.ingested_at, event.content_sha256, event.sensitivity,
                   canonical_json(event.to_dict()), event.event_id))
                if result.rowcount != 1:
                    raise ValueError(
                        f"source event not found during redaction refresh: {event.event_id}")

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_artifact(self, artifact_id: str, kind: str, path: str, size_bytes: int) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)",
                         (artifact_id, kind, path, size_bytes, utc_now()))

    def save_snapshot(self, snapshot_id: str, manifest_path: str, input_path: str,
                      event_count: int) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)",
                         (snapshot_id, utc_now(), manifest_path, input_path, event_count))

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        return dict(row) if row else None

    def save_model_run(self, values: dict) -> None:
        columns = ("run_id", "snapshot_id", "provider", "model", "prompt_version", "status",
                   "duration_ms", "exit_code", "input_tokens", "output_tokens", "cost_usd",
                   "output_path", "stderr_path", "validation_json", "attempts")
        with self.connect() as conn:
            conn.execute(f"INSERT INTO model_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         tuple(values.get(column) for column in columns))

    def model_runs_for(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM model_runs WHERE run_id=? ORDER BY provider", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_evaluation(self, run_id: str, provider: str, reviewer: str,
                        scores: dict[str, int], accepted: bool,
                        notes: str | None = None) -> int:
        with self.connect() as conn:
            result = conn.execute(
                "INSERT INTO evaluations "
                "(run_id,provider,reviewer,scores_json,accepted,notes,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, provider, reviewer, canonical_json(scores), int(accepted),
                 notes or None, utc_now()),
            )
            return int(result.lastrowid)

    def risk_reviews(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM risk_reviews").fetchall()
        return {row["risk_id"]: dict(row) for row in rows}

    def save_risk_review(self, risk_id: str, snapshot_id: str, provider: str,
                         status: str, evidence_event_id: str | None,
                         reviewer: str, notes: str) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO risk_reviews
              (risk_id,snapshot_id,provider,status,evidence_event_id,reviewer,notes,reviewed_at)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(risk_id) DO UPDATE SET
              status=excluded.status, evidence_event_id=excluded.evidence_event_id,
              reviewer=excluded.reviewer, notes=excluded.notes,
              reviewed_at=excluded.reviewed_at""",
              (risk_id, snapshot_id, provider, status, evidence_event_id,
               reviewer, notes or None, utc_now()))

    def paid_model_calls_today(self, today_utc: str) -> int:
        with self.connect() as conn:
            row = conn.execute("""SELECT COALESCE(SUM(CASE
                WHEN m.duration_ms>=1000 OR m.input_tokens IS NOT NULL
                     OR m.output_tokens IS NOT NULL OR m.cost_usd IS NOT NULL
                THEN m.attempts ELSE 0 END),0) FROM model_runs m
              JOIN runs r ON r.run_id=m.run_id
              WHERE substr(r.started_at,1,10)=? AND m.model NOT LIKE 'fake-%'""",
                               (today_utc,)).fetchone()
        return int(row[0])

    def backup(self, directory: Path, keep_days: int = 7) -> Path:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = directory / f"ai2593-{stamp}.db"
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        destination.chmod(0o600)
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        for candidate in directory.glob("ai2593-*.db"):
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                candidate.unlink()
        return destination

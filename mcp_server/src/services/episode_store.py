"""SQLite job journal. Contains private episode payloads; never expose payloads in status."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class EpisodeStore:
    def __init__(self, path: Path | None):
        self.path = path
        self._memory = sqlite3.connect(':memory:') if path is None else None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT NOT NULL UNIQUE, group_id TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT
                );
            """)
        if path:
            os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = self._memory
        if db is None:
            if self.path is None:
                raise RuntimeError('Episode store has no database')
            db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            if self._memory is None:
                db.close()

    def row(self, uuid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE uuid=?', (uuid,)).fetchone()
        return dict(row) if row else None

    def require(self, uuid: str) -> dict[str, Any]:
        row = self.row(uuid)
        if row is None:
            raise ValueError('Episode job not found')
        return row

    def submit(self, payload: dict[str, Any], implicit_time: bool) -> tuple[dict, bool]:
        uuid = payload['uuid']
        with self.connect() as db:
            old = db.execute('SELECT * FROM jobs WHERE uuid=?', (uuid,)).fetchone()
            if old:
                previous = json.loads(old['payload'])
                if implicit_time:
                    payload['reference_time'] = previous['reference_time']
                if previous != payload:
                    raise ValueError('Episode UUID payload conflict')
                return dict(old), False
            encoded = json.dumps(payload, sort_keys=True)
            db.execute(
                'INSERT INTO jobs (uuid,group_id,payload,status) VALUES (?,?,?,?)',
                (uuid, payload['group_id'], encoded, 'queued'),
            )
        return self.require(uuid), True

    def recover(self) -> list[dict]:
        with self.connect() as db:
            # Attempts are retained across restart; a crash is not a free new retry budget.
            db.execute("UPDATE jobs SET status='queued' WHERE status='processing'")
            rows = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY seq").fetchall()
        return [dict(row) for row in rows]

    def update(self, uuid: str, status: str, error: str | None = None, attempt=False):
        with self.connect() as db:
            db.execute(
                'UPDATE jobs SET status=?,error=?,attempts=attempts+? WHERE uuid=?',
                (status, error, int(attempt), uuid),
            )

    def status(self, uuid: str, group_id: str) -> dict:
        row = self.row(uuid)
        if row is None or row['group_id'] != group_id:
            raise ValueError('Episode job not found')
        return {key: row[key] for key in ('uuid', 'group_id', 'status', 'attempts', 'error')}

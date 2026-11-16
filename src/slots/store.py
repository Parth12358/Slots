"""On-disk state: one SQLite file under the state directory.

The daemon is the only writer. Leases and waiters are rewritten in one transaction
after every change (there are tens of rows, not thousands), so the file always
holds one consistent picture. Events and admin actions are appended.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease (id TEXT PRIMARY KEY, row TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS waiter (token TEXT PRIMARY KEY, row TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, row TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, row TEXT NOT NULL);
"""
EVENTS_KEPT = 5000


class Store:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.db = self._open()
        except sqlite3.DatabaseError:
            broken = path.with_name(f"{path.name}.broken-{int(time.time())}")
            os.replace(path, broken)
            self.db = self._open()
            self.recovered_from = broken
        else:
            self.recovered_from = None

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        db.execute("SELECT count(*) FROM lease").fetchone()
        return db

    def load(self) -> dict:
        seq = self.db.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
        return {
            "seq": int(seq[0]) if seq else 0,
            "leases": [json.loads(r) for (r,) in self.db.execute("SELECT row FROM lease")],
            "waiters": [json.loads(r) for (r,) in self.db.execute("SELECT row FROM waiter")],
        }

    def save(self, rows: dict, events: list[dict] = ()):
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('seq', ?)", (str(rows["seq"]),))
            db.execute("DELETE FROM lease")
            db.executemany("INSERT INTO lease VALUES (?, ?)", [(l["id"], json.dumps(l)) for l in rows["leases"]])
            db.execute("DELETE FROM waiter")
            db.executemany("INSERT INTO waiter VALUES (?, ?)", [(w["token"], json.dumps(w)) for w in rows["waiters"]])
            db.executemany("INSERT INTO event (ts, row) VALUES (?, ?)", [(e["ts"], json.dumps(e)) for e in events])
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def log_admin(self, entry: dict):
        self.db.execute("INSERT INTO admin_log (ts, row) VALUES (?, ?)", (time.time(), json.dumps(entry)))

    def recent_events(self, n: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT row FROM event ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [json.loads(r) for (r,) in reversed(rows)]

    def trim(self):
        self.db.execute("DELETE FROM event WHERE id <= (SELECT max(id) FROM event) - ?", (EVENTS_KEPT,))

    def close(self):
        self.db.close()

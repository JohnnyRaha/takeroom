"""The library.

One row per take, holding everything needed to reproduce it: the exact style
prompt and lyrics that were sent to the model, the seed, and the full parameter
dict. If you can't re-render a take from its row, the row is incomplete.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS takes (
    id              TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',

    brief           TEXT NOT NULL DEFAULT '',   -- what the user typed
    style_prompt    TEXT NOT NULL DEFAULT '',   -- what was actually sent
    lyrics          TEXT NOT NULL DEFAULT '',
    duration        REAL NOT NULL DEFAULT 0,
    seed            INTEGER,
    params          TEXT NOT NULL DEFAULT '{}', -- json
    backend         TEXT NOT NULL DEFAULT '',

    raw_path        TEXT NOT NULL DEFAULT '',
    master_path     TEXT NOT NULL DEFAULT '',
    mp3_path        TEXT NOT NULL DEFAULT '',
    stem_dir        TEXT NOT NULL DEFAULT '',

    input_lufs      REAL,
    output_lufs     REAL,
    peak_dbfs       REAL,
    render_seconds  REAL,

    parent_id       TEXT,                       -- set for extends / re-rolls
    starred         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_takes_created ON takes(created_at DESC);
"""

_JSON_FIELDS = {"params"}


class Library:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- writes -------------------------------------------------------------

    def create(self, **fields: Any) -> str:
        take_id = fields.pop("id", None) or uuid.uuid4().hex[:12]
        row = {
            "id": take_id,
            "created_at": time.time(),
            "status": "queued",
        }
        row.update(self._encode(fields))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT INTO takes ({cols}) VALUES ({marks})", list(row.values()))
            self._conn.commit()
        return take_id

    def update(self, take_id: str, **fields: Any) -> None:
        if not fields:
            return
        enc = self._encode(fields)
        assignments = ", ".join(f"{k} = ?" for k in enc)
        with self._lock:
            self._conn.execute(
                f"UPDATE takes SET {assignments} WHERE id = ?", [*enc.values(), take_id]
            )
            self._conn.commit()

    def delete(self, take_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM takes WHERE id = ?", (take_id,))
            self._conn.commit()

    # -- reads --------------------------------------------------------------

    def get(self, take_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,))
            row = cur.fetchone()
        return self._decode(row) if row else None

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM takes ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = cur.fetchall()
        return [self._decode(r) for r in rows]

    def reset_orphans(self) -> int:
        """Jobs left mid-flight by a crash or restart are not resumable."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE takes SET status='failed', error='interrupted by restart' "
                "WHERE status IN ('queued','running')"
            )
            self._conn.commit()
            return cur.rowcount

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _encode(fields: dict[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        for key in _JSON_FIELDS & out.keys():
            out[key] = json.dumps(out[key], ensure_ascii=False)
        return out

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for key in _JSON_FIELDS:
            if key in out:
                try:
                    out[key] = json.loads(out[key] or "{}")
                except json.JSONDecodeError:
                    out[key] = {}
        out["starred"] = bool(out.get("starred"))
        for key in ("raw_path", "master_path", "mp3_path", "stem_dir"):
            out[f"has_{key.replace('_path', '').replace('_dir', '')}"] = bool(out.get(key))
        return out


def as_public(take: dict[str, Any]) -> dict[str, Any]:
    """Strip absolute filesystem paths before sending to the browser."""
    hidden: Iterable[str] = ("raw_path", "master_path", "mp3_path", "stem_dir")
    public = {k: v for k, v in take.items() if k not in hidden}
    public["has_master"] = bool(take.get("master_path"))
    public["has_mp3"] = bool(take.get("mp3_path"))
    public["has_stems"] = bool(take.get("stem_dir"))
    public["has_midi"] = bool(take.get("midi"))
    return public

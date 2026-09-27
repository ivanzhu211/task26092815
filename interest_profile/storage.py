"""SQLite 持久化层：建表与底层读写，不含业务规则。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    max_ts REAL
);
CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY,
    ext_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    max_ts REAL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    identity_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ts REAL NOT NULL,
    category TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_identity ON events(identity_id);
CREATE TABLE IF NOT EXISTS identity_cat (
    identity_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    anchor REAL NOT NULL,
    s_short REAL NOT NULL,
    s_long REAL NOT NULL,
    PRIMARY KEY (identity_id, category)
);
CREATE TABLE IF NOT EXISTS profile_cat (
    profile_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    anchor REAL NOT NULL,
    s_short REAL NOT NULL,
    s_long REAL NOT NULL,
    PRIMARY KEY (profile_id, category)
);
"""

IdentityRow = Tuple[int, str, str, int, Optional[float]]  # id, ext_id, kind, profile_id, max_ts
CatRow = Tuple[str, float, float, float]  # category, anchor, s_short, s_long


class Storage:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ---- profiles ----
    def create_profile(self) -> int:
        cur = self._conn.execute("INSERT INTO profiles(max_ts) VALUES (NULL)")
        return int(cur.lastrowid)

    def delete_profile(self, profile_id: int) -> None:
        self._conn.execute("DELETE FROM profile_cat WHERE profile_id = ?", (profile_id,))
        self._conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    def get_profile_max_ts(self, profile_id: int) -> Optional[float]:
        row = self._conn.execute(
            "SELECT max_ts FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return row[0] if row else None

    def set_profile_max_ts(self, profile_id: int, max_ts: Optional[float]) -> None:
        self._conn.execute(
            "UPDATE profiles SET max_ts = ? WHERE id = ?", (max_ts, profile_id)
        )

    # ---- identities ----
    def get_identity(self, ext_id: str) -> Optional[IdentityRow]:
        return self._conn.execute(
            "SELECT id, ext_id, kind, profile_id, max_ts FROM identities WHERE ext_id = ?",
            (ext_id,),
        ).fetchone()

    def create_identity(self, ext_id: str, kind: str, profile_id: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO identities(ext_id, kind, profile_id, max_ts) VALUES (?,?,?,NULL)",
            (ext_id, kind, profile_id),
        )
        return int(cur.lastrowid)

    def move_identity(self, identity_id: int, profile_id: int) -> None:
        self._conn.execute(
            "UPDATE identities SET profile_id = ? WHERE id = ?",
            (profile_id, identity_id),
        )

    def identities_of_profile(self, profile_id: int) -> List[IdentityRow]:
        return self._conn.execute(
            "SELECT id, ext_id, kind, profile_id, max_ts FROM identities WHERE profile_id = ?",
            (profile_id,),
        ).fetchall()

    def set_identity_max_ts(self, identity_id: int, max_ts: float) -> None:
        self._conn.execute(
            "UPDATE identities SET max_ts = ? WHERE id = ?", (max_ts, identity_id)
        )

    # ---- events ----
    def insert_event(self, event_id: str, identity_id: int, event_type: str,
                     ts: float, category: Optional[str]) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events(event_id, identity_id, event_type, ts, category)"
            " VALUES (?,?,?,?,?)",
            (event_id, identity_id, event_type, ts, category),
        )
        return cur.rowcount > 0

    # ---- category aggregates ----
    def get_identity_cat(self, identity_id: int, category: str) -> Optional[CatRow]:
        row = self._conn.execute(
            "SELECT category, anchor, s_short, s_long FROM identity_cat"
            " WHERE identity_id = ? AND category = ?",
            (identity_id, category),
        ).fetchone()
        return tuple(row) if row else None

    def upsert_identity_cat(self, identity_id: int, category: str,
                            anchor: float, s_short: float, s_long: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO identity_cat(identity_id, category, anchor, s_short, s_long)"
            " VALUES (?,?,?,?,?)",
            (identity_id, category, anchor, s_short, s_long),
        )

    def identity_cats(self, identity_id: int) -> List[CatRow]:
        return [tuple(r) for r in self._conn.execute(
            "SELECT category, anchor, s_short, s_long FROM identity_cat WHERE identity_id = ?",
            (identity_id,),
        )]

    def get_profile_cat(self, profile_id: int, category: str) -> Optional[CatRow]:
        row = self._conn.execute(
            "SELECT category, anchor, s_short, s_long FROM profile_cat"
            " WHERE profile_id = ? AND category = ?",
            (profile_id, category),
        ).fetchone()
        return tuple(row) if row else None

    def upsert_profile_cat(self, profile_id: int, category: str,
                           anchor: float, s_short: float, s_long: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO profile_cat(profile_id, category, anchor, s_short, s_long)"
            " VALUES (?,?,?,?,?)",
            (profile_id, category, anchor, s_short, s_long),
        )

    def profile_cats(self, profile_id: int) -> List[CatRow]:
        return [tuple(r) for r in self._conn.execute(
            "SELECT category, anchor, s_short, s_long FROM profile_cat WHERE profile_id = ?",
            (profile_id,),
        )]

    def replace_profile_cats(self, profile_id: int, rows: List[CatRow]) -> None:
        self._conn.execute("DELETE FROM profile_cat WHERE profile_id = ?", (profile_id,))
        self._conn.executemany(
            "INSERT INTO profile_cat(profile_id, category, anchor, s_short, s_long)"
            " VALUES (?,?,?,?,?)",
            [(profile_id, c, a, s, l) for c, a, s, l in rows],
        )

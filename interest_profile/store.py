"""SQLite 持久化层。

表结构：
- identities(identity, kind, profile_id)：标识及其当前所属画像
- profiles(profile_id)：存在的画像
- identity_cat(identity, category, anchor, sum_short, sum_long, event_count)：
  标识级类别聚合，anchor 为该标识该类别事件的最大时间戳，
  sum_* 为相对 anchor 的衰减权重和
- profile_cat(profile_id, category, anchor, sum_short, sum_long, event_count)：
  画像级类别聚合，结构同上
- events(event_id)：已接收事件 ID，用于去重（只算第一次）
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

from . import scoring

#: 一条类别聚合记录：(anchor, sum_short, sum_long, event_count)
CatAgg = Tuple[float, float, float, int]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    identity   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    profile_id INTEGER NOT NULL,
    max_ts     REAL
);
CREATE INDEX IF NOT EXISTS idx_identities_profile ON identities(profile_id);

CREATE TABLE IF NOT EXISTS profiles (
    profile_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS identity_cat (
    identity    TEXT NOT NULL,
    category    TEXT NOT NULL,
    anchor      REAL NOT NULL,
    sum_short   REAL NOT NULL,
    sum_long    REAL NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY (identity, category)
);

CREATE TABLE IF NOT EXISTS profile_cat (
    profile_id  INTEGER NOT NULL,
    category    TEXT NOT NULL,
    anchor      REAL NOT NULL,
    sum_short   REAL NOT NULL,
    sum_long    REAL NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY (profile_id, category)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY
);
"""


def _shift(agg: CatAgg, new_anchor: float) -> CatAgg:
    """把聚合的参考点平移到更晚的 new_anchor。"""
    anchor, sum_short, sum_long, count = agg
    if new_anchor <= anchor:
        return agg
    dt = new_anchor - anchor
    return (
        new_anchor,
        sum_short * scoring.decay(dt, scoring.SHORT_HALF_LIFE),
        sum_long * scoring.decay(dt, scoring.LONG_HALF_LIFE),
        count,
    )


def combine_aggs(left: CatAgg, right: CatAgg) -> CatAgg:
    """合并两条同类别的聚合记录。"""
    anchor = max(left[0], right[0])
    left = _shift(left, anchor)
    right = _shift(right, anchor)
    return (
        anchor,
        left[1] + right[1],
        left[2] + right[2],
        left[3] + right[3],
    )


def add_event_to_agg(agg: Optional[CatAgg], ts: float, weight: float) -> CatAgg:
    """向聚合中加入一个事件（可乱序，ts 为 Unix 秒，weight 为基础权重）。"""
    if agg is None:
        return (ts, weight, weight, 1)
    anchor = max(agg[0], ts)
    agg = _shift(agg, anchor)
    dt = anchor - ts
    return (
        anchor,
        agg[1] + weight * scoring.decay(dt, scoring.SHORT_HALF_LIFE),
        agg[2] + weight * scoring.decay(dt, scoring.LONG_HALF_LIFE),
        agg[3] + 1,
    )


def sum_identity_cats(
    store: "Store", identities: List[str]
) -> Dict[str, CatAgg]:
    """把若干标识的类别聚合按类别求和（全部为加法，无对消）。"""
    result: Dict[str, CatAgg] = {}
    for identity in identities:
        for category, agg in store.identity_cats(identity).items():
            existing = result.get(category)
            result[category] = (
                agg if existing is None else combine_aggs(existing, agg)
            )
    return result

class Store:
    """对 SQLite 文件的读写封装。调用方串行使用。"""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        """显式事务：块内所有写操作原子提交。"""
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- 标识与画像 ----

    def ensure_identity(self, identity: str, kind: str) -> int:
        """标识首次出现时建档并分配独立画像，返回其画像 ID。"""
        row = self._conn.execute(
            "SELECT profile_id FROM identities WHERE identity = ?", (identity,)
        ).fetchone()
        if row is not None:
            return row[0]
        profile_id = self.create_profile()
        self._conn.execute(
            "INSERT INTO identities (identity, kind, profile_id) VALUES (?, ?, ?)",
            (identity, kind, profile_id),
        )
        return profile_id

    def get_identity(self, identity: str) -> Optional[Tuple[str, int]]:
        """返回 (kind, profile_id)，不存在返回 None。"""
        row = self._conn.execute(
            "SELECT kind, profile_id FROM identities WHERE identity = ?", (identity,)
        ).fetchone()
        return (row[0], row[1]) if row is not None else None

    def create_profile(self) -> int:
        cur = self._conn.execute("INSERT INTO profiles DEFAULT VALUES")
        return cur.lastrowid

    def delete_profile(self, profile_id: int) -> None:
        self._conn.execute("DELETE FROM profiles WHERE profile_id = ?", (profile_id,))
        self._conn.execute(
            "DELETE FROM profile_cat WHERE profile_id = ?", (profile_id,)
        )

    def move_identity(self, identity: str, profile_id: int) -> None:
        self._conn.execute(
            "UPDATE identities SET profile_id = ? WHERE identity = ?",
            (profile_id, identity),
        )

    def profile_identities(self, profile_id: int) -> List[Tuple[str, str]]:
        """画像下全部 (identity, kind)。"""
        rows = self._conn.execute(
            "SELECT identity, kind FROM identities WHERE profile_id = ? ORDER BY identity",
            (profile_id,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def profile_has_customer(self, profile_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM identities WHERE profile_id = ? AND kind = ? LIMIT 1",
            (profile_id, "customer"),
        ).fetchone()
        return row is not None

    def touch_identity_ts(self, identity: str, ts: float) -> None:
        """把标识的最大事件时间推进到 ts（只增不减）。"""
        self._conn.execute(
            "UPDATE identities SET max_ts = MAX(COALESCE(max_ts, ?), ?) "
            "WHERE identity = ?",
            (ts, ts, identity),
        )

    def profile_max_ts(self, profile_id: int) -> Optional[float]:
        """画像内全部标识的最大事件时间；无任何事件返回 None。"""
        row = self._conn.execute(
            "SELECT MAX(max_ts) FROM identities WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        return row[0] if row is not None else None

    # ---- 事件去重 ----

    def record_event(self, event_id: str) -> bool:
        """首次出现返回 True，重复返回 False。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events (event_id) VALUES (?)", (event_id,)
        )
        return cur.rowcount > 0

    # ---- 类别聚合 ----

    def add_identity_cat(
        self, identity: str, category: str, ts: float, weight: float
    ) -> None:
        """向标识级聚合加入一个事件的贡献（内部处理 anchor 平移）。"""
        agg = self._get_cat("identity_cat", "identity", identity, category)
        agg = add_event_to_agg(agg, ts, weight)
        self._put_cat("identity_cat", "identity", identity, category, agg)

    def add_profile_cat(
        self, profile_id: int, category: str, ts: float, weight: float
    ) -> None:
        agg = self._get_cat("profile_cat", "profile_id", profile_id, category)
        agg = add_event_to_agg(agg, ts, weight)
        self._put_cat("profile_cat", "profile_id", profile_id, category, agg)

    def _get_cat(
        self, table: str, key_col: str, key, category: str
    ) -> Optional[CatAgg]:
        row = self._conn.execute(
            f"SELECT anchor, sum_short, sum_long, event_count FROM {table} "
            f"WHERE {key_col} = ? AND category = ?",
            (key, category),
        ).fetchone()
        return (row[0], row[1], row[2], row[3]) if row is not None else None

    def _put_cat(
        self, table: str, key_col: str, key, category: str, agg: CatAgg
    ) -> None:
        self._conn.execute(
            f"INSERT INTO {table} ({key_col}, category, anchor, sum_short, sum_long, event_count) "
            f"VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT ({key_col}, category) DO UPDATE SET "
            f"anchor = excluded.anchor, sum_short = excluded.sum_short, "
            f"sum_long = excluded.sum_long, event_count = excluded.event_count",
            (key, category, agg[0], agg[1], agg[2], agg[3]),
        )

    def identity_cats(self, identity: str) -> Dict[str, CatAgg]:
        rows = self._conn.execute(
            "SELECT category, anchor, sum_short, sum_long, event_count "
            "FROM identity_cat WHERE identity = ?",
            (identity,),
        ).fetchall()
        return {row[0]: (row[1], row[2], row[3], row[4]) for row in rows}

    def profile_cats(self, profile_id: int) -> Dict[str, CatAgg]:
        rows = self._conn.execute(
            "SELECT category, anchor, sum_short, sum_long, event_count "
            "FROM profile_cat WHERE profile_id = ?",
            (profile_id,),
        ).fetchall()
        return {row[0]: (row[1], row[2], row[3], row[4]) for row in rows}

    def merge_profile_cats(self, src_profile_id: int, dst_profile_id: int) -> None:
        """把 src 画像的类别聚合并入 dst（调用方保证随后删除 src）。"""
        src_cats = self.profile_cats(src_profile_id)
        if not src_cats:
            return
        dst_cats = self.profile_cats(dst_profile_id)
        for category, src_agg in src_cats.items():
            dst_agg = dst_cats.get(category)
            merged = src_agg if dst_agg is None else combine_aggs(dst_agg, src_agg)
            self._put_cat("profile_cat", "profile_id", dst_profile_id, category, merged)

    def recompute_profile_cats(self, profile_id: int) -> None:
        """从画像当前成员的标识级聚合重算画像聚合（纯加法，避免对消误差）。"""
        members = [
            identity
            for identity, _kind in self.profile_identities(profile_id)
        ]
        self.set_profile_cats(profile_id, sum_identity_cats(self, members))

    def set_profile_cats(self, profile_id: int, cats: Dict[str, CatAgg]) -> None:
        """整体覆盖一个画像的类别聚合。"""
        self._conn.execute(
            "DELETE FROM profile_cat WHERE profile_id = ?", (profile_id,)
        )
        self._conn.executemany(
            "INSERT INTO profile_cat (profile_id, category, anchor, sum_short, sum_long, event_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (profile_id, category, agg[0], agg[1], agg[2], agg[3])
                for category, agg in cats.items()
            ],
        )

    def all_profile_ids(self) -> List[int]:
        rows = self._conn.execute("SELECT profile_id FROM profiles").fetchall()
        return [row[0] for row in rows]

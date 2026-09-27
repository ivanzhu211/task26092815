"""业务逻辑层：事件入库、关联、合并、撤销、查询。

核心思路：衰减和按类别增量维护。对半衰期 H，类别聚合行保存
(anchor, S)，其中 S = Σ w·2**((t_e - anchor)/H)，anchor 为该类别已见
事件的最大时间。查询时刻 t 的分数 = S·2**((anchor - t)/H)。
因此入库、关联、合并、撤销均为 O(类别数)，查询为 O(画像类别数)。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from interest_profile.models import (
    AssociationError,
    CategoryScore,
    DissociationError,
    Event,
    KIND_ANONYMOUS,
    KIND_CUSTOMER,
    MergeError,
    ProfileError,
    ProfileResult,
    QueryTimeError,
    UnknownIdentityError,
    WEIGHTS,
    HALF_LIFE_SHORT_DAYS,
    HALF_LIFE_LONG_DAYS,
    FUSED_SHORT_RATIO,
    FUSED_LONG_RATIO,
    normalize_category,
    parse_timestamp,
)
from interest_profile.storage import Storage

_HALF_SHORT_S = HALF_LIFE_SHORT_DAYS * 86400.0
_HALF_LONG_S = HALF_LIFE_LONG_DAYS * 86400.0


def _add_to_row(row: Optional[Tuple[float, float, float]], weight: float,
                ts: float) -> Tuple[float, float, float]:
    """把一条事件的贡献并入 (anchor, s_short, s_long)。"""
    if row is None:
        return (ts, weight, weight)
    anchor, s_short, s_long = row
    if ts > anchor:
        f_short = 2.0 ** ((anchor - ts) / _HALF_SHORT_S)
        f_long = 2.0 ** ((anchor - ts) / _HALF_LONG_S)
        return (ts, s_short * f_short + weight, s_long * f_long + weight)
    return (
        anchor,
        s_short + weight * 2.0 ** ((ts - anchor) / _HALF_SHORT_S),
        s_long + weight * 2.0 ** ((ts - anchor) / _HALF_LONG_S),
    )


def _combine_rows(a: Tuple[float, float, float],
                  b: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """合并同一类别的两个聚合行（满足结合律/交换律）。"""
    anchor = max(a[0], b[0])
    s_short = (a[1] * 2.0 ** ((a[0] - anchor) / _HALF_SHORT_S)
               + b[1] * 2.0 ** ((b[0] - anchor) / _HALF_SHORT_S))
    s_long = (a[2] * 2.0 ** ((a[0] - anchor) / _HALF_LONG_S)
              + b[2] * 2.0 ** ((b[0] - anchor) / _HALF_LONG_S))
    return (anchor, s_short, s_long)


class InterestProfileStore:
    """客户兴趣画像库入口。调用方串行调用，无需考虑并发。"""

    def __init__(self, db_path: str) -> None:
        self._storage = Storage(db_path)

    def close(self) -> None:
        self._storage.close()

    def __enter__(self) -> "InterestProfileStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部 ----
    def _get_or_create_identity(self, ext_id: str, kind: str,
                                error_cls=ProfileError) -> Tuple[int, str, str, int, Optional[float]]:
        row = self._storage.get_identity(ext_id)
        if row is not None:
            if row[2] != kind:
                raise error_cls(f"标识 {ext_id!r} 的类型是 {row[2]!r}，不是 {kind!r}")
            return row
        profile_id = self._storage.create_profile()
        self._storage.create_identity(ext_id, kind, profile_id)
        return self._storage.get_identity(ext_id)

    def _recompute_profile(self, profile_id: int) -> None:
        """从成员标识的 identity_cat 行重算画像聚合与 max_ts。"""
        members = self._storage.identities_of_profile(profile_id)
        acc: Dict[str, Tuple[float, float, float]] = {}
        max_ts: Optional[float] = None
        for m in members:
            m_ts = m[4]
            if m_ts is not None and (max_ts is None or m_ts > max_ts):
                max_ts = m_ts
            for cat, anchor, s_short, s_long in self._storage.identity_cats(m[0]):
                cur = acc.get(cat)
                new = (anchor, s_short, s_long)
                acc[cat] = new if cur is None else _combine_rows(cur, new)
        rows = [(cat, a, s, l) for cat, (a, s, l) in acc.items()]
        self._storage.replace_profile_cats(profile_id, rows)
        self._storage.set_profile_max_ts(profile_id, max_ts)

    # ---- 事件入库 ----
    def ingest(self, events: Iterable) -> int:
        """批量入库事件（乱序、重复均可）。返回实际新入库条数。"""
        accepted = 0
        with self._storage.transaction():
            for ev in events:
                if isinstance(ev, Event):
                    event_id, identity, kind = ev.event_id, ev.identity, ev.identity_kind
                    event_type, timestamp, category = ev.event_type, ev.timestamp, ev.category
                else:
                    event_id = ev["event_id"]
                    identity = ev["identity"]
                    kind = ev["identity_kind"]
                    event_type = ev["event_type"]
                    timestamp = ev["timestamp"]
                    category = ev.get("category")
                ts = parse_timestamp(timestamp).timestamp()
                cat = normalize_category(category)
                ident = self._get_or_create_identity(identity, kind)
                ident_id, _, _, profile_id, ident_max_ts = ident
                if not self._storage.insert_event(event_id, ident_id, event_type, ts, cat):
                    continue  # 重复事件：只算第一次
                accepted += 1
                if ident_max_ts is None or ts > ident_max_ts:
                    self._storage.set_identity_max_ts(ident_id, ts)
                    prof_max = self._storage.get_profile_max_ts(profile_id)
                    if prof_max is None or ts > prof_max:
                        self._storage.set_profile_max_ts(profile_id, ts)
                weight = WEIGHTS.get(event_type)
                if weight is None or cat is None:
                    continue
                row = self._storage.get_identity_cat(ident_id, cat)
                row = row[1:] if row else None
                a, s, l = _add_to_row(row, weight, ts)
                self._storage.upsert_identity_cat(ident_id, cat, a, s, l)
                prow = self._storage.get_profile_cat(profile_id, cat)
                prow = prow[1:] if prow else None
                a, s, l = _add_to_row(prow, weight, ts)
                self._storage.upsert_profile_cat(profile_id, cat, a, s, l)
        return accepted

    # ---- 关联 ----
    def associate(self, anonymous_id: str, customer_id: str) -> None:
        """把匿名 ID 关联到客户；已关联到同一画像则无变化，关联到别的画像报错。"""
        with self._storage.transaction():
            anon = self._get_or_create_identity(anonymous_id, KIND_ANONYMOUS, AssociationError)
            cust = self._get_or_create_identity(customer_id, KIND_CUSTOMER, AssociationError)
            if anon[3] == cust[3]:
                return
            members = self._storage.identities_of_profile(anon[3])
            if len(members) > 1:
                raise AssociationError(
                    f"匿名标识 {anonymous_id!r} 已关联到其他画像，不能再次关联")
            old_profile = anon[3]
            self._storage.move_identity(anon[0], cust[3])
            self._storage.delete_profile(old_profile)
            self._recompute_profile(cust[3])

    # ---- 合并 ----
    def merge(self, source_customer_id: str, target_customer_id: str) -> None:
        """把 source 客户所在画像并入 target 客户所在画像（仅限客户 ID，不可撤销）。"""
        with self._storage.transaction():
            src = self._get_or_create_identity(source_customer_id, KIND_CUSTOMER, MergeError)
            tgt = self._get_or_create_identity(target_customer_id, KIND_CUSTOMER, MergeError)
            if src[3] == tgt[3]:
                return
            for ident in self._storage.identities_of_profile(src[3]):
                self._storage.move_identity(ident[0], tgt[3])
            self._storage.delete_profile(src[3])
            self._recompute_profile(tgt[3])

    # ---- 撤销关联 ----
    def dissociate(self, anonymous_id: str) -> None:
        """撤销匿名 ID 的关联：它的全部事件随它回到只含它自己的匿名画像。"""
        with self._storage.transaction():
            row = self._storage.get_identity(anonymous_id)
            if row is None or row[2] != KIND_ANONYMOUS:
                raise DissociationError(f"标识 {anonymous_id!r} 不是已知的匿名标识")
            members = self._storage.identities_of_profile(row[3])
            if len(members) == 1:
                raise DissociationError(f"匿名标识 {anonymous_id!r} 当前未关联，不能撤销")
            old_profile = row[3]
            new_profile = self._storage.create_profile()
            self._storage.move_identity(row[0], new_profile)
            self._recompute_profile(new_profile)
            self._recompute_profile(old_profile)

    # ---- 查询 ----
    def query(self, identity: str, at) -> ProfileResult:
        """用任一标识查询其当前所属画像在时刻 at 的兴趣分数。"""
        row = self._storage.get_identity(identity)
        if row is None:
            raise UnknownIdentityError(f"未知标识: {identity!r}")
        t = parse_timestamp(at).timestamp()
        profile_id = row[3]
        max_ts = self._storage.get_profile_max_ts(profile_id)
        if max_ts is not None and t < max_ts:
            raise QueryTimeError(
                f"查询时刻 {t} 早于画像中的事件时间 {max_ts}")
        categories: Dict[str, CategoryScore] = {}
        for cat, anchor, s_short, s_long in self._storage.profile_cats(profile_id):
            short = s_short * 2.0 ** ((anchor - t) / _HALF_SHORT_S)
            long = s_long * 2.0 ** ((anchor - t) / _HALF_LONG_S)
            categories[cat] = CategoryScore(
                short=short, long=long,
                fused=FUSED_SHORT_RATIO * short + FUSED_LONG_RATIO * long)
        return ProfileResult(profile_id=profile_id, categories=categories)

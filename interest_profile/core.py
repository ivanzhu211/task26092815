"""客户兴趣画像服务：上层客户数据平台直接调用的库入口。"""

from __future__ import annotations

from typing import Dict, Iterable, List

from . import scoring
from .errors import (
    AlreadyLinkedError,
    IdentityKindError,
    NotLinkedError,
    QueryTimeError,
    UnknownIdentityError,
)
from .models import KIND_ANONYMOUS, KIND_CUSTOMER, CategoryScore, Event, Profile
from .store import Store

#: 单批事件数量上限
MAX_BATCH_SIZE = 5000


class InterestProfileService:
    """客户兴趣画像模块。

    用法::

        svc = InterestProfileService("profiles.db")
        svc.ingest([...])            # 批量上报事件，每批最多 5000 条
        svc.link("anon-1", "cust-1") # 匿名ID关联到客户
        svc.merge("cust-b", "cust-a")# B 所在画像并入 A 所在画像
        svc.unlink("anon-1")         # 撤销匿名ID关联
        profile = svc.query("cust-1", "2026-09-28T00:00:00+08:00")
        svc.close()
    """

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "InterestProfileService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def ingest(self, events: Iterable[Event]) -> int:
        """批量入库事件，返回实际计入的事件数（去重后）。每批最多 5000 条。"""
        events = list(events)
        if len(events) > MAX_BATCH_SIZE:
            raise ValueError(f"单批事件最多 {MAX_BATCH_SIZE} 条，收到 {len(events)} 条")
        accepted = 0
        with self._store.transaction():
            for event in events:
                if not self._store.record_event(event.event_id):
                    continue
                ts = scoring.parse_instant(event.occurred_at)
                profile_id = self._store.ensure_identity(
                    event.identity, event.identity_kind
                )
                self._store.touch_identity_ts(event.identity, ts)
                weight = scoring.event_weight(event.event_type)
                category = scoring.normalize_category(event.category)
                if weight > 0.0 and category is not None:
                    self._store.add_identity_cat(
                        event.identity, category, ts, weight
                    )
                    self._store.add_profile_cat(profile_id, category, ts, weight)
                accepted += 1
        return accepted

    def link(self, anonymous_id: str, customer_id: str) -> None:
        """把匿名ID关联到客户；已关联到别的画像报错，关联到同一画像无变化。"""
        with self._store.transaction():
            anon_profile = self._store.ensure_identity(anonymous_id, KIND_ANONYMOUS)
            cust_profile = self._store.ensure_identity(customer_id, KIND_CUSTOMER)
            self._require_kind(anonymous_id, KIND_ANONYMOUS)
            self._require_kind(customer_id, KIND_CUSTOMER)
            if anon_profile == cust_profile:
                return
            if self._store.profile_has_customer(anon_profile):
                raise AlreadyLinkedError(
                    f"匿名ID {anonymous_id!r} 已关联到其他画像"
                )
            self._store.move_identity(anonymous_id, cust_profile)
            self._store.merge_profile_cats(anon_profile, cust_profile)
            self._store.delete_profile(anon_profile)

    def merge(self, customer_b: str, customer_a: str) -> None:
        """B 所在画像并入 A 所在画像，仅限客户ID，不可撤销。"""
        with self._store.transaction():
            profile_b = self._store.ensure_identity(customer_b, KIND_CUSTOMER)
            profile_a = self._store.ensure_identity(customer_a, KIND_CUSTOMER)
            self._require_kind(customer_b, KIND_CUSTOMER)
            self._require_kind(customer_a, KIND_CUSTOMER)
            if profile_b == profile_a:
                return
            for identity, _kind in self._store.profile_identities(profile_b):
                self._store.move_identity(identity, profile_a)
            self._store.merge_profile_cats(profile_b, profile_a)
            self._store.delete_profile(profile_b)

    def unlink(self, anonymous_id: str) -> None:
        """撤销匿名ID关联，其全部事件随它回到只含它自己的匿名画像。"""
        with self._store.transaction():
            found = self._store.get_identity(anonymous_id)
            if found is None:
                raise NotLinkedError(f"标识 {anonymous_id!r} 不存在，未关联")
            kind, old_profile = found
            if kind != KIND_ANONYMOUS:
                raise IdentityKindError(
                    f"标识 {anonymous_id!r} 是客户ID，不能撤销关联"
                )
            if not self._store.profile_has_customer(old_profile):
                raise NotLinkedError(f"匿名ID {anonymous_id!r} 未关联到任何客户")
            new_profile = self._store.create_profile()
            self._store.move_identity(anonymous_id, new_profile)
            self._store.recompute_profile_cats(old_profile)
            self._store.set_profile_cats(
                new_profile, self._store.identity_cats(anonymous_id)
            )

    def query(self, identity: str, at: str) -> Profile:
        """用任一标识查询其当前所属画像在 at 时刻的分数。"""
        found = self._store.get_identity(identity)
        if found is None:
            raise UnknownIdentityError(f"标识 {identity!r} 不存在")
        _kind, profile_id = found
        t = scoring.parse_instant(at)
        max_ts = self._store.profile_max_ts(profile_id)
        if max_ts is not None and t < max_ts:
            raise QueryTimeError(
                f"查询时刻 {at!r} 早于画像中的事件时间"
            )
        cats = self._store.profile_cats(profile_id)
        scores = []
        for category in sorted(cats):
            anchor, sum_short, sum_long, _count = cats[category]
            dt = t - anchor
            short = sum_short * scoring.decay(dt, scoring.SHORT_HALF_LIFE)
            long = sum_long * scoring.decay(dt, scoring.LONG_HALF_LIFE)
            scores.append(
                CategoryScore(
                    category=category,
                    short_score=short,
                    long_score=long,
                    fused_score=scoring.SHORT_FUSION * short
                    + scoring.LONG_FUSION * long,
                )
            )
        identities = tuple(
            identity for identity, _kind in self._store.profile_identities(profile_id)
        )
        return Profile(
            profile_id=profile_id, identities=identities, scores=tuple(scores)
        )

    def _require_kind(self, identity: str, kind: str) -> None:
        found = self._store.get_identity(identity)
        if found is not None and found[0] != kind:
            raise IdentityKindError(
                f"标识 {identity!r} 的类型是 {found[0]!r}，操作要求 {kind!r}"
            )

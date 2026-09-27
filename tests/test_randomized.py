"""随机差分测试：乱序/重复事件、关联、多级合并、撤销、重启随机交替执行，
每一步之后把所有画像与按当前归属从原始事件重算的参考结果比对。
"""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from interest_profile import (
    AlreadyLinkedError,
    Event,
    IdentityKindError,
    InterestProfileService,
    NotLinkedError,
)
from interest_profile import scoring

REL_TOL = 1e-9
ABS_TOL = 1e-12

BASE = datetime(2005, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 28, tzinfo=timezone(timedelta(hours=8)))
SPAN_SECONDS = (NOW - BASE).total_seconds()

EVENT_TYPES = ["purchase", "page_view", "email_open", "click", "scroll", ""]
CATEGORIES = [
    "books", "Books", " BOOKS ", "sports", "Sports", "tech", "fashion",
    "food", "travel", "music", "games", "news", "finance", "health",
    "pets", None, "", "   ",
]


def random_ts(rng):
    dt = BASE + timedelta(seconds=rng.uniform(0, SPAN_SECONDS))
    offset = rng.choice([-8, 0, 8])
    return dt.astimezone(timezone(timedelta(hours=offset)))


class RefModel:
    """参考模型：保存原始事件与归属，查询时从头计算。"""

    def __init__(self):
        self.events = {}
        self.kinds = {}
        self.member_of = {}
        self.profiles = {}
        self._next_pid = 1

    def _ensure(self, identity, kind):
        if identity not in self.kinds:
            self.kinds[identity] = kind
            pid = self._next_pid
            self._next_pid += 1
            self.member_of[identity] = pid
            self.profiles[pid] = {identity}

    def _profile_has_customer(self, pid):
        return any(self.kinds[i] == "customer" for i in self.profiles[pid])

    def ingest(self, event, ts_seconds):
        if event.event_id in self.events:
            return
        self.events[event.event_id] = (
            event.identity,
            event.event_type,
            ts_seconds,
            event.category,
        )
        self._ensure(event.identity, event.identity_kind)

    def link(self, anon, cust):
        self._ensure(anon, "anonymous")
        self._ensure(cust, "customer")
        if self.kinds[anon] != "anonymous" or self.kinds[cust] != "customer":
            return IdentityKindError
        pa, pc = self.member_of[anon], self.member_of[cust]
        if pa == pc:
            return None
        if self._profile_has_customer(pa):
            return AlreadyLinkedError
        self.profiles[pc].add(anon)
        self.profiles[pa].discard(anon)
        if not self.profiles[pa]:
            del self.profiles[pa]
        self.member_of[anon] = pc
        return None

    def merge(self, b, a):
        self._ensure(b, "customer")
        self._ensure(a, "customer")
        if self.kinds[b] != "customer" or self.kinds[a] != "customer":
            return IdentityKindError
        pb, pa = self.member_of[b], self.member_of[a]
        if pb == pa:
            return None
        for identity in self.profiles[pb]:
            self.member_of[identity] = pa
        self.profiles[pa] |= self.profiles[pb]
        del self.profiles[pb]
        return None

    def unlink(self, anon):
        if anon not in self.kinds:
            return NotLinkedError
        if self.kinds[anon] != "anonymous":
            return IdentityKindError
        pid = self.member_of[anon]
        if not self._profile_has_customer(pid):
            return NotLinkedError
        new_pid = self._next_pid
        self._next_pid += 1
        self.profiles[pid].discard(anon)
        self.profiles[new_pid] = {anon}
        self.member_of[anon] = new_pid
        return None

    def expected_scores(self, identity, t_seconds):
        """从原始事件重算 identity 当前所属画像在 t 时刻的分数。"""
        pid = self.member_of[identity]
        members = self.profiles[pid]
        per_cat = {}
        for ev_identity, ev_type, ts, category in self.events.values():
            if ev_identity not in members:
                continue
            weight = scoring.event_weight(ev_type)
            cat = scoring.normalize_category(category)
            if weight == 0.0 or cat is None:
                continue
            per_cat.setdefault(cat, []).append((weight, ts))
        result = {}
        for cat, items in per_cat.items():
            short = math.fsum(
                w * 0.5 ** ((t_seconds - ts) / scoring.SHORT_HALF_LIFE)
                for w, ts in items
            )
            long = math.fsum(
                w * 0.5 ** ((t_seconds - ts) / scoring.LONG_HALF_LIFE)
                for w, ts in items
            )
            result[cat] = (short, long, 0.7 * short + 0.3 * long)
        return result


def assert_close(actual, expected, label):
    if abs(expected) < ABS_TOL:
        assert abs(actual - expected) <= ABS_TOL, (
            f"{label}: {actual!r} vs {expected!r} (abs)"
        )
    else:
        assert abs(actual - expected) <= REL_TOL * abs(expected), (
            f"{label}: {actual!r} vs {expected!r} (rel)"
        )


def compare_all(service, model, query_time):
    t_seconds = query_time.timestamp()
    for identity in sorted(model.kinds):
        profile = service.query(identity, query_time.isoformat())
        expected = model.expected_scores(identity, t_seconds)
        got = {s.category: s for s in profile.scores}
        assert set(got) == set(expected), (
            f"标识 {identity} 类别集合不一致: "
            f"{sorted(got)} vs {sorted(expected)}"
        )
        for cat, (e_short, e_long, e_fused) in expected.items():
            s = got[cat]
            assert_close(s.short_score, e_short, f"{identity}/{cat}/short")
            assert_close(s.long_score, e_long, f"{identity}/{cat}/long")
            assert_close(s.fused_score, e_fused, f"{identity}/{cat}/fused")
        pid = model.member_of[identity]
        assert set(profile.identities) == set(model.profiles[pid])


def run_random_sequence(seed, steps, tmp_path):
    rng = random.Random(seed)
    db_path = str(tmp_path / "p.db")
    service = InterestProfileService(db_path)
    model = RefModel()
    customers = [f"cust-{i}" for i in range(8)]
    anons = [f"anon-{i}" for i in range(16)]
    event_counter = [0]

    def make_event():
        if rng.random() < 0.25 and event_counter[0] > 0:
            event_id = f"ev-{rng.randrange(event_counter[0])}"
        else:
            event_id = f"ev-{event_counter[0]}"
            event_counter[0] += 1
        identity = rng.choice(customers + anons)
        kind = "customer" if identity.startswith("cust") else "anonymous"
        dt = random_ts(rng)
        event = Event(
            event_id=event_id,
            identity=identity,
            identity_kind=kind,
            event_type=rng.choice(EVENT_TYPES),
            occurred_at=dt.isoformat(),
            category=rng.choice(CATEGORIES),
        )
        return event, dt

    try:
        for _step in range(steps):
            op = rng.random()
            if op < 0.55:
                batch = [make_event() for _ in range(rng.randint(1, 25))]
                for event, dt in batch:
                    model.ingest(event, dt.timestamp())
                service.ingest([event for event, _ in batch])
            elif op < 0.70:
                anon, cust = rng.choice(anons), rng.choice(customers)
                expected_err = model.link(anon, cust)
                if expected_err is None:
                    service.link(anon, cust)
                else:
                    with pytest.raises(expected_err):
                        service.link(anon, cust)
            elif op < 0.82:
                b, a = rng.choice(customers), rng.choice(customers)
                expected_err = model.merge(b, a)
                if expected_err is None:
                    service.merge(b, a)
                else:
                    with pytest.raises(expected_err):
                        service.merge(b, a)
            elif op < 0.92:
                anon = rng.choice(anons)
                expected_err = model.unlink(anon)
                if expected_err is None:
                    service.unlink(anon)
                else:
                    with pytest.raises(expected_err):
                        service.unlink(anon)
            else:
                service.close()
                service = InterestProfileService(db_path)
            query_time = NOW + timedelta(days=rng.uniform(0, 60))
            compare_all(service, model, query_time)
    finally:
        service.close()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_randomized_against_reference(seed, tmp_path):
    run_random_sequence(seed, steps=120, tmp_path=tmp_path)

"""随机交替操作 + 参考模型比对测试。

每步操作后，把库中所有画像与「按当前归属从原始事件从头算」的参考
结果比对：类别集合完全一致；分数相对误差 <= 1e-9，参考值绝对值
小于 1e-12 时按绝对误差 1e-12 判定。
"""
import os
import random
import tempfile
from datetime import datetime, timezone

import pytest

from interest_profile import (
    AssociationError,
    DissociationError,
    InterestProfileStore,
    KIND_ANONYMOUS,
    KIND_CUSTOMER,
)

T_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
T_MIN = datetime(2005, 1, 1, tzinfo=timezone.utc).timestamp()
T_MAX = datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp()
WEIGHTS = {"purchase": 5.0, "page_view": 1.0, "email_open": 0.5}
EVENT_TYPES = ["purchase", "page_view", "page_view", "email_open", "click", "scroll"]
CATEGORIES = (["sports", "music", "tech", "finance", "travel", "food", "games",
               "fashion", "health", "news", " Sports ", "TECH", "", "   ", None]
              + [f"cat{i}" for i in range(20)])


def norm_cat(c):
    if c is None:
        return None
    c = str(c).strip().lower()
    return c or None


class Model:
    """参考模型：直接按业务规则维护归属，查询时从原始事件重算。"""

    def __init__(self):
        self.kind = {}            # ident -> kind
        self.profile_of = {}      # ident -> pid
        self.members = {}         # pid -> set(ident)
        self.events = {}          # event_id -> (ident, etype, ts, cat)
        self.ident_events = {}    # ident -> [(etype, ts, cat)]
        self.next_pid = 1

    def _create(self, ident, kind):
        if ident in self.kind:
            assert self.kind[ident] == kind, "kind mismatch"
            return
        self.kind[ident] = kind
        pid = self.next_pid
        self.next_pid += 1
        self.profile_of[ident] = pid
        self.members[pid] = {ident}
        self.ident_events[ident] = []

    def ingest(self, batch):
        for e in batch:
            if e["event_id"] in self.events:
                continue
            ident = e["identity"]
            self._create(ident, e["identity_kind"])
            cat = norm_cat(e.get("category"))
            ts = e["timestamp"].timestamp()
            self.events[e["event_id"]] = (ident, e["event_type"], ts, cat)
            self.ident_events[ident].append((e["event_type"], ts, cat))

    def associate(self, anon, cust):
        self._create(anon, KIND_ANONYMOUS)
        self._create(cust, KIND_CUSTOMER)
        pa, pc = self.profile_of[anon], self.profile_of[cust]
        if pa == pc:
            return "noop"
        if len(self.members[pa]) > 1:
            return "error"
        self.members[pc].add(anon)
        del self.members[pa]
        self.profile_of[anon] = pc
        return "ok"

    def merge(self, src, tgt):
        self._create(src, KIND_CUSTOMER)
        self._create(tgt, KIND_CUSTOMER)
        ps, pt = self.profile_of[src], self.profile_of[tgt]
        if ps == pt:
            return
        for ident in self.members[ps]:
            self.profile_of[ident] = pt
        self.members[pt] |= self.members[ps]
        del self.members[ps]

    def dissociate(self, anon):
        if self.kind.get(anon) != KIND_ANONYMOUS:
            return "error"
        pid = self.profile_of[anon]
        if len(self.members[pid]) == 1:
            return "error"
        self.members[pid].discard(anon)
        new_pid = self.next_pid
        self.next_pid += 1
        self.members[new_pid] = {anon}
        self.profile_of[anon] = new_pid
        return "ok"

    def expected_categories(self, ident, t):
        """从原始事件重算 ident 当前所属画像的各类别分数。"""
        pid = self.profile_of[ident]
        acc = {}
        for member in self.members[pid]:
            for etype, ts, cat in self.ident_events[member]:
                w = WEIGHTS.get(etype)
                if w is None or cat is None:
                    continue
                age_s = (t - ts) / 86400.0
                cur = acc.setdefault(cat, [0.0, 0.0])
                cur[0] += w * 0.5 ** (age_s / 7.0)
                cur[1] += w * 0.5 ** (age_s / 90.0)
        return {c: (s, l, 0.7 * s + 0.3 * l) for c, (s, l) in acc.items()}


def assert_close(got, ref):
    if abs(ref) < 1e-12:
        assert abs(got - ref) <= 1e-12
    else:
        assert abs(got - ref) <= 1e-9 * abs(ref), f"{got} vs {ref}"


def check_all(store, model, t):
    pid_map = {}
    for ident in model.kind:
        result = store.query(ident, t)
        expected = model.expected_categories(ident, t.timestamp())
        assert set(result.categories) == set(expected), (
            f"{ident}: {sorted(result.categories)} != {sorted(expected)}")
        for cat, (s, l, f) in expected.items():
            sc = result.categories[cat]
            assert_close(sc.short, s)
            assert_close(sc.long, l)
            assert_close(sc.fused, f)
        # 同一模型画像的标识必须查到同一画像，不同画像必须不同
        mpid = model.profile_of[ident]
        if mpid in pid_map:
            assert pid_map[mpid] == result.profile_id
        else:
            pid_map[mpid] = result.profile_id
    assert len(set(pid_map.values())) == len(pid_map)


def random_batch(rng, model):
    known_ids = list(model.events)
    batch = []
    for _ in range(rng.randint(1, 30)):
        if known_ids and rng.random() < 0.15:
            eid = rng.choice(known_ids)  # 重复上报
        else:
            eid = f"e{rng.getrandbits(40)}"
        kind = KIND_ANONYMOUS if rng.random() < 0.7 else KIND_CUSTOMER
        prefix = "anon" if kind == KIND_ANONYMOUS else "cust"
        ident = f"{prefix}{rng.randrange(30 if kind == KIND_ANONYMOUS else 10)}"
        ts = datetime.fromtimestamp(rng.uniform(T_MIN, T_MAX), tz=timezone.utc)
        batch.append({
            "event_id": eid,
            "identity": ident,
            "identity_kind": kind,
            "event_type": rng.choice(EVENT_TYPES),
            "timestamp": ts,
            "category": rng.choice(CATEGORIES),
        })
    return batch


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_randomized_against_model(seed):
    rng = random.Random(seed)
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    model = Model()
    store = InterestProfileStore(path)
    try:
        for step in range(300):
            op = rng.random()
            if op < 0.45:
                batch = random_batch(rng, model)
                assert store.ingest(batch) <= len(batch)
                model.ingest(batch)
            elif op < 0.60:
                anon = f"anon{rng.randrange(30)}"
                cust = f"cust{rng.randrange(10)}"
                outcome = model.associate(anon, cust)
                if outcome == "error":
                    with pytest.raises(AssociationError):
                        store.associate(anon, cust)
                else:
                    store.associate(anon, cust)
            elif op < 0.72:
                src = f"cust{rng.randrange(10)}"
                tgt = f"cust{rng.randrange(10)}"
                model.merge(src, tgt)
                store.merge(src, tgt)
            elif op < 0.87:
                anon = f"anon{rng.randrange(30)}"
                outcome = model.dissociate(anon)
                if outcome == "error":
                    with pytest.raises(DissociationError):
                        store.dissociate(anon)
                else:
                    store.dissociate(anon)
            else:
                store.close()  # 模拟进程重启
                store = InterestProfileStore(path)
            check_all(store, model, T_NOW)
    finally:
        store.close()

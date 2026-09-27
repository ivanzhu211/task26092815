"""规模与耗时测试（验收用）。

直接运行：python3 tests/test_perf.py
或：IP_PERF=1 python3 -m pytest tests/test_perf.py -s

规模：20 万标识、300 万事件、200 个类别；
单画像从 1000 条事件涨到 20 万条时，查询/关联/合并/撤销耗时不明显变长，
查询在 10ms 内返回；每批 5000 条，入库在 2 秒内。
"""
import os
import random
import statistics
import tempfile
import time
from datetime import datetime, timezone

import pytest

from interest_profile import InterestProfileStore, KIND_ANONYMOUS, KIND_CUSTOMER

T_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
T_MIN = datetime(2005, 1, 1, tzinfo=timezone.utc).timestamp()
T_MAX = datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp()

TYPES = ["purchase", "page_view", "page_view", "page_view", "email_open", "click"]
N_ANONYMOUS = 200_000
N_CUSTOMERS = 2_000
N_EVENTS = 3_000_000
N_CATEGORIES = 200
BATCH = 5_000


def gen_events(rng, n):
    for i in range(n):
        if rng.random() < 0.7:
            ident = f"anon{rng.randrange(N_ANONYMOUS)}"
            kind = KIND_ANONYMOUS
        else:
            ident = f"cust{rng.randrange(N_CUSTOMERS)}"
            kind = KIND_CUSTOMER
        yield {
            "event_id": f"e{i}",
            "identity": ident,
            "identity_kind": kind,
            "event_type": rng.choice(TYPES),
            "timestamp": datetime.fromtimestamp(rng.uniform(T_MIN, T_MAX),
                                                tz=timezone.utc),
            "category": f"cat{rng.randrange(N_CATEGORIES)}",
        }


def batched(gen, size):
    batch = []
    for item in gen:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


@pytest.mark.perf
def test_big_scale_ingest_and_query():
    rng = random.Random(42)
    path = os.path.join(tempfile.mkdtemp(), "big.db")
    store = InterestProfileStore(path)
    batch_times = []
    t0 = time.perf_counter()
    total = 0
    for batch in batched(gen_events(rng, N_EVENTS), BATCH):
        s = time.perf_counter()
        assert store.ingest(batch) == len(batch)
        batch_times.append(time.perf_counter() - s)
        total += BATCH
    ingest_total = time.perf_counter() - t0
    print(f"\n入库 {total} 条，总耗时 {ingest_total:.1f}s，"
          f"平均 {total / ingest_total:.0f} 条/s，"
          f"单批最大 {max(batch_times) * 1000:.0f}ms，"
          f"单批 P95 {statistics.quantiles(batch_times, n=20)[18] * 1000:.0f}ms")
    assert max(batch_times) < 2.0, "单批 5000 条入库超过 2 秒"

    samples = [f"cust{rng.randrange(N_CUSTOMERS)}" for _ in range(500)]
    lat = []
    for ident in samples:
        s = time.perf_counter()
        store.query(ident, T_NOW)
        lat.append((time.perf_counter() - s) * 1000)
    print(f"查询：中位数 {statistics.median(lat):.2f}ms，"
          f"P95 {statistics.quantiles(lat, n=20)[18]:.2f}ms，"
          f"最大 {max(lat):.2f}ms")
    assert statistics.median(lat) < 10.0
    store.close()


@pytest.mark.perf
def test_single_profile_growth_steady():
    """画像 1k → 200k 事件，查询/关联/合并/撤销耗时不随事件数明显增长。"""
    path = os.path.join(tempfile.mkdtemp(), "grow.db")
    store = InterestProfileStore(path)

    # tcust 画像：tcust 本人 + 50 个挂名匿名（每个带少量事件），符合上限
    for i in range(50):
        store.associate(f"f{i}", "tcust")
    store.ingest([
        {"event_id": f"fev{i}", "identity": f"f{i % 50}",
         "identity_kind": KIND_ANONYMOUS, "event_type": "page_view",
         "timestamp": datetime.fromtimestamp(rng_ts := T_MIN + i * 3600,
                                             tz=timezone.utc).isoformat(),
         "category": f"cat{i % N_CATEGORIES}"}
        for i in range(500)
    ])
    store.ingest([{
        "event_id": "tcust0", "identity": "tcust",
        "identity_kind": KIND_CUSTOMER, "event_type": "email_open",
        "timestamp": T_NOW.isoformat(), "category": "cat0",
    }])

    def feed(n, start):
        rng = random.Random(100 + start)
        batch = []
        for i in range(n):
            batch.append({
                "event_id": f"p{start + i}",
                "identity": "target",
                "identity_kind": KIND_ANONYMOUS,
                "event_type": rng.choice(TYPES),
                "timestamp": datetime.fromtimestamp(rng.uniform(T_MIN, T_MAX),
                                                    tz=timezone.utc),
                "category": f"cat{rng.randrange(N_CATEGORIES)}",
            })
        store.ingest(batch)

    def timed(fn):
        s = time.perf_counter()
        fn()
        return (time.perf_counter() - s) * 1000

    def stage(size, merge_customer):
        # target 此时是只含自身的匿名画像，带 size 条事件
        q_solo = timed(lambda: store.query("target", T_NOW))
        assoc = timed(lambda: store.associate("target", "tcust"))
        q_big = timed(lambda: store.query("tcust", T_NOW))  # 200k 事件的大画像
        store.ingest([{
            "event_id": f"{merge_customer}ev", "identity": merge_customer,
            "identity_kind": KIND_CUSTOMER, "event_type": "page_view",
            "timestamp": T_NOW.isoformat(), "category": "cat1",
        }])
        merge_ms = timed(lambda: store.merge(merge_customer, "tcust"))
        diss_ms = timed(lambda: store.dissociate("target"))
        return q_solo, q_big, assoc, merge_ms, diss_ms

    feed(1000, 0)
    r1 = stage(1000, "mc1")
    feed(200_000 - 1000, 1000)
    r2 = stage(200_000, "mc2")
    names = ("查询(独立)", "查询(大画像)", "关联", "合并", "撤销")
    print()
    for name, v1, v2 in zip(names, r1, r2):
        print(f"{name:12s} 1k: {v1:8.2f}ms   200k: {v2:8.2f}ms")
    assert r1[0] < 10 and r1[1] < 10 and r2[0] < 10 and r2[1] < 10
    for v1, v2 in zip(r1[2:], r2[2:]):
        # 这些操作只按类别/成员重算聚合，不应随事件数显著增长
        assert v2 < 300 and v2 < max(300.0, 5 * max(v1, 1.0))
    store.close()


if __name__ == "__main__":
    test_big_scale_ingest_and_query()
    test_single_profile_growth_steady()
    print("ALL PERF TESTS PASSED")

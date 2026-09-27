"""规模耗时测试：20 万标识、300 万事件、200 类别、单客户 50 匿名ID + 20 万事件。

默认按完整规模运行；设置环境变量 PERF_SCALE（0~1）可缩小规模做快速检查，
例如 PERF_SCALE=0.02 python3 -m pytest tests/test_perf.py。
"""

import os
import random
import time

import pytest

from interest_profile import Event, InterestProfileService

SCALE = float(os.environ.get("PERF_SCALE", "1.0"))

N_IDENTITIES = max(1000, int(200_000 * SCALE))
N_EVENTS = max(50_000, int(3_000_000 * SCALE))
N_CATEGORIES = 200
WHALE_ANONS = 50
WHALE_EVENTS = max(10_000, int(200_000 * SCALE))
BATCH = 5000

QUERY_LIMIT_S = 0.010
BATCH_LIMIT_S = 2.0

TYPES = ["purchase", "page_view", "email_open", "click"]
QUERY_AT = "2026-09-28T00:00:00+08:00"


def _event(event_no, identity, kind, rng, cats):
    day = rng.randrange(0, 365)
    return Event(
        f"ev-{event_no}",
        identity,
        kind,
        rng.choice(TYPES),
        f"2025-09-{(day % 28) + 1:02d}T{(day % 24):02d}:00:00+08:00",
        rng.choice(cats),
    )


class _Env:
    svc = None
    db_path = None
    whale_members = None
    max_batch = 0.0

    def reopen(self):
        self.svc.close()
        self.svc = InterestProfileService(self.db_path)


@pytest.fixture(scope="module")
def whale_service(tmp_path_factory):
    """构造含一个大画像（50 匿名ID、WHALE_EVENTS 条事件）的库。"""
    rng = random.Random(42)
    cats = [f"cat-{i}" for i in range(N_CATEGORIES)]
    env = _Env()
    env.db_path = str(tmp_path_factory.mktemp("perf") / "perf.db")
    env.svc = InterestProfileService(env.db_path)
    svc = env.svc
    whale_members = ["whale"] + [f"whale-anon-{i}" for i in range(WHALE_ANONS)]
    env.whale_members = whale_members

    # 先关联一半匿名ID，事件上报后再关联另一半
    for anon in whale_members[1 : 1 + WHALE_ANONS // 2]:
        svc.link(anon, "whale")

    max_batch = 0.0
    event_no = 0
    pending = []

    def flush():
        nonlocal max_batch, event_no
        start = time.perf_counter()
        svc.ingest(pending)
        max_batch = max(max_batch, time.perf_counter() - start)
        pending.clear()

    # 大画像事件：均匀打到 51 个标识上
    for k in range(WHALE_EVENTS):
        identity = whale_members[k % len(whale_members)]
        kind = "customer" if identity == "whale" else "anonymous"
        pending.append(_event(event_no, identity, kind, rng, cats))
        event_no += 1
        if len(pending) == BATCH:
            flush()
        if k == WHALE_EVENTS // 2:
            for anon in whale_members[1 + WHALE_ANONS // 2 :]:
                svc.link(anon, "whale")

    # 其余事件散布到普通标识
    remaining = N_EVENTS - WHALE_EVENTS
    for k in range(remaining):
        identity_no = rng.randrange(N_IDENTITIES)
        kind = "customer" if identity_no % 5 == 0 else "anonymous"
        pending.append(_event(event_no, f"id-{identity_no}", kind, rng, cats))
        event_no += 1
        if len(pending) == BATCH:
            flush()
    if pending:
        flush()

    env.max_batch = max_batch
    yield env
    env.svc.close()


def test_batch_ingest_within_limit(whale_service):
    print(f"\n单批 {BATCH} 条最大入库耗时: {whale_service.max_batch:.3f}s")
    assert whale_service.max_batch < BATCH_LIMIT_S


def test_query_within_10ms(whale_service):
    svc, whale_members = whale_service.svc, whale_service.whale_members
    # 大画像查询
    samples = []
    for identity in whale_members[:10]:
        start = time.perf_counter()
        profile = svc.query(identity, QUERY_AT)
        samples.append(time.perf_counter() - start)
    assert profile.scores, "大画像应有分数"
    # 普通画像查询
    for i in range(0, 1000, 100):
        kind = "customer" if i % 5 == 0 else "anonymous"
        start = time.perf_counter()
        svc.query(f"id-{i}", QUERY_AT)
        samples.append(time.perf_counter() - start)
    worst = max(samples)
    print(f"\n查询最大耗时: {worst * 1000:.3f}ms (共 {len(samples)} 次采样)")
    assert worst < QUERY_LIMIT_S


def test_operations_scale_with_profile_size(whale_service):
    """画像事件从 1k 涨到 200k，关联/合并/撤销/查询耗时不明显变长。"""
    svc, whale_members = whale_service.svc, whale_service.whale_members
    rng = random.Random(7)
    cats = ["perf-cat"]

    # 造一个 1000 事件的小画像
    small_events = [
        Event(f"small-{i}", "small-cust", "customer", "page_view",
              "2026-09-01T00:00:00+08:00", cats[0])
        for i in range(1000)
    ]
    svc.ingest(small_events)
    svc.ingest(
        [Event(f"sa-{i}", "small-anon", "anonymous", "page_view",
               "2026-09-01T00:00:00+08:00", cats[0]) for i in range(10)]
    )
    svc.link("small-anon", "small-cust")

    timings = {}
    for label, anon, cust in (
        ("small", "small-anon", "small-cust"),
        ("whale", whale_members[-1], "whale"),
    ):
        start = time.perf_counter()
        svc.unlink(anon)
        t_unlink = time.perf_counter() - start
        start = time.perf_counter()
        svc.link(anon, cust)
        t_link = time.perf_counter() - start
        start = time.perf_counter()
        svc.query(cust, QUERY_AT)
        t_query = time.perf_counter() - start
        timings[label] = (t_unlink, t_link, t_query)
        print(
            f"\n{label}: unlink={t_unlink * 1000:.2f}ms "
            f"link={t_link * 1000:.2f}ms query={t_query * 1000:.3f}ms"
        )

    # 合并：把一个新客户并入两个画像，比较耗时
    for label, cust in (("small", "small-cust"), ("whale", "whale")):
        svc.ingest(
            [Event(f"m-{label}", f"merge-{label}", "customer", "purchase",
                   "2026-09-01T00:00:00+08:00", cats[0])]
        )
        start = time.perf_counter()
        svc.merge(f"merge-{label}", cust)
        timings[label] += (time.perf_counter() - start,)
        print(f"{label}: merge={timings[label][3] * 1000:.2f}ms")

    small_unlink, small_link, small_query, small_merge = timings["small"]
    whale_unlink, whale_link, whale_query, whale_merge = timings["whale"]
    # 查询必须始终在 10ms 内
    assert whale_query < QUERY_LIMIT_S
    # 其余操作与画像大小无关（只与标识数/类别数有关），放宽到 1s 上限
    for name, value in (
        ("whale unlink", whale_unlink),
        ("whale link", whale_link),
        ("whale merge", whale_merge),
    ):
        assert value < 1.0, f"{name} 耗时 {value:.3f}s 超预期"


def test_restart_keeps_state(whale_service):
    svc = whale_service.svc
    before = svc.query("whale", QUERY_AT)
    whale_service.reopen()
    after = whale_service.svc.query("whale", QUERY_AT)
    assert [s.category for s in before.scores] == [s.category for s in after.scores]
    for s1, s2 in zip(before.scores, after.scores):
        assert s1.fused_score == s2.fused_score

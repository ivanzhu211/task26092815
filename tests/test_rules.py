"""业务规则单元测试。"""
import os
import tempfile

import pytest

from interest_profile import (
    AssociationError,
    DissociationError,
    Event,
    InterestProfileStore,
    KIND_ANONYMOUS,
    KIND_CUSTOMER,
    MergeError,
    QueryTimeError,
    UnknownIdentityError,
)

DAY = 86400.0


@pytest.fixture()
def store():
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    with InterestProfileStore(path) as s:
        yield s


def ev(event_id, identity, kind, etype, ts, category=None):
    return Event(event_id, identity, kind, etype, ts, category)


def decay(w, age_days, half_days):
    return w * 0.5 ** (age_days / half_days)


# ---- 权重 / 类别归一化 ----
def test_weights_and_normalization(store):
    t0 = "2026-09-28T00:00:00Z"
    store.ingest([
        ev("e1", "c1", KIND_CUSTOMER, "purchase", t0, " Sports "),
        ev("e2", "c1", KIND_CUSTOMER, "page_view", t0, "SPORTS"),
        ev("e3", "c1", KIND_CUSTOMER, "email_open", t0, "sports"),
        ev("e4", "c1", KIND_CUSTOMER, "other", t0, "sports"),     # 类型不计
        ev("e5", "c1", KIND_CUSTOMER, "purchase", t0, "   "),      # 空类别
        ev("e6", "c1", KIND_CUSTOMER, "purchase", t0),             # 缺类别
    ])
    r = store.query("c1", t0)
    assert set(r.categories) == {"sports"}
    sc = r.categories["sports"]
    assert sc.short == pytest.approx(5 + 1 + 0.5)
    assert sc.long == pytest.approx(5 + 1 + 0.5)
    assert sc.fused == pytest.approx(0.7 * sc.short + 0.3 * sc.long)


def test_short_long_fused_decay(store):
    store.ingest([
        ev("e1", "c1", KIND_CUSTOMER, "page_view", "2026-09-21T00:00:00Z", "music"),
        ev("e2", "c1", KIND_CUSTOMER, "purchase", "2026-07-01T00:00:00Z", "music"),
    ])
    q = "2026-09-28T00:00:00Z"
    r = store.query("c1", q)
    sc = r.categories["music"]
    assert sc.short == pytest.approx(decay(1, 7, 7) + decay(5, 89, 7), rel=1e-12)
    assert sc.long == pytest.approx(decay(1, 7, 90) + decay(5, 89, 90), rel=1e-12)
    assert sc.fused == pytest.approx(0.7 * sc.short + 0.3 * sc.long, rel=1e-12)


def test_categories_only_real_ones(store):
    store.ingest([
        ev("e1", "c1", KIND_CUSTOMER, "page_view", "2026-09-28T00:00:00Z", "a"),
        ev("e2", "c1", KIND_CUSTOMER, "unknown_type", "2026-09-28T00:00:00Z", "b"),
        ev("e3", "c1", KIND_CUSTOMER, "page_view", "2026-09-28T00:00:00Z", ""),
    ])
    assert set(store.query("c1", "2026-09-28T00:00:00Z").categories) == {"a"}


# ---- 去重 / 乱序 ----
def test_duplicate_event_first_wins(store):
    store.ingest([
        ev("e1", "a1", KIND_ANONYMOUS, "purchase", "2026-09-28T00:00:00Z", "x"),
        ev("e1", "c1", KIND_CUSTOMER, "purchase", "2026-09-28T00:00:00Z", "y"),
    ])
    assert set(store.query("a1", "2026-09-28T00:00:00Z").categories) == {"x"}
    assert store.query("c1", "2026-09-28T00:00:00Z").categories == {}


def test_duplicate_in_later_batch(store):
    store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "page_view",
                     "2026-09-20T00:00:00Z", "x")])
    n = store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "page_view",
                         "2026-09-20T00:00:00Z", "x")])
    assert n == 0
    sc = store.query("a1", "2026-09-28T00:00:00Z").categories["x"]
    assert sc.short == pytest.approx(decay(1, 8, 7), rel=1e-12)


def test_out_of_order_events(store):
    store.ingest([
        ev("e3", "c1", KIND_CUSTOMER, "page_view", "2026-09-20T00:00:00Z", "x"),
        ev("e1", "c1", KIND_CUSTOMER, "page_view", "2026-01-01T00:00:00Z", "x"),
        ev("e2", "c1", KIND_CUSTOMER, "purchase", "2026-09-25T00:00:00Z", "x"),
    ])
    r = store.query("c1", "2026-09-28T00:00:00Z")
    expect = decay(1, 8, 7) + decay(1, 270, 7) + decay(5, 3, 7)
    assert r.categories["x"].short == pytest.approx(expect, rel=1e-12)


def test_auto_create_on_first_event(store):
    store.ingest([ev("e1", "brand-new", KIND_ANONYMOUS, "page_view",
                     "2026-09-28T00:00:00Z", "x")])
    assert "x" in store.query("brand-new", "2026-09-28T00:00:00Z").categories


# ---- 查询时刻 ----
def test_query_earlier_than_latest_event_errors(store):
    store.ingest([ev("e1", "c1", KIND_CUSTOMER, "page_view",
                     "2026-09-28T12:00:00Z", "x")])
    with pytest.raises(QueryTimeError):
        store.query("c1", "2026-09-28T00:00:00Z")
    store.query("c1", "2026-09-28T12:00:00Z")  # 恰等于事件时间可以


def test_query_unknown_identity(store):
    with pytest.raises(UnknownIdentityError):
        store.query("nope", "2026-09-28T00:00:00Z")


def test_empty_profile_query(store):
    store.associate("a1", "c1")
    assert store.query("a1", "2026-09-28T00:00:00Z").categories == {}


# ---- 关联 ----
def test_associate_moves_past_and_future_events(store):
    store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "purchase",
                     "2026-09-20T00:00:00Z", "x")])
    store.associate("a1", "c1")
    r = store.query("c1", "2026-09-28T00:00:00Z")
    assert r.categories["x"].short == pytest.approx(decay(5, 8, 7), rel=1e-12)
    assert store.query("a1", "2026-09-28T00:00:00Z").profile_id == r.profile_id
    store.ingest([ev("e2", "a1", KIND_ANONYMOUS, "page_view",
                     "2026-09-27T00:00:00Z", "y")])
    assert "y" in store.query("c1", "2026-09-28T00:00:00Z").categories


def test_associate_same_profile_noop(store):
    store.associate("a1", "c1")
    store.associate("a1", "c1")  # 不报错


def test_associate_to_other_profile_errors(store):
    store.associate("a1", "c1")
    with pytest.raises(AssociationError):
        store.associate("a1", "c2")


def test_associate_kind_mismatch(store):
    store.ingest([ev("e0", "c1", KIND_CUSTOMER, "page_view",
                     "2026-09-28T00:00:00Z", "x")])
    store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "page_view",
                     "2026-09-28T00:00:00Z", "x")])
    with pytest.raises(AssociationError):
        store.associate("c1", "c2")  # c1 是客户，不能当匿名 ID 关联
    with pytest.raises(AssociationError):
        store.associate("a1", "a1")  # a1 是匿名，不能当客户


# ---- 合并 ----
def test_merge_profiles(store):
    store.associate("a1", "c1")
    store.associate("a2", "c2")
    store.ingest([
        ev("e1", "a1", KIND_ANONYMOUS, "purchase", "2026-09-20T00:00:00Z", "x"),
        ev("e2", "c2", KIND_CUSTOMER, "page_view", "2026-09-21T00:00:00Z", "x"),
    ])
    store.merge("c2", "c1")  # c2 并入 c1
    r = store.query("c1", "2026-09-28T00:00:00Z")
    assert "x" in r.categories
    assert store.query("c2", "2026-09-28T00:00:00Z").profile_id == r.profile_id
    assert store.query("a2", "2026-09-28T00:00:00Z").profile_id == r.profile_id
    expect = decay(5, 8, 7) + decay(1, 7, 7)
    assert r.categories["x"].short == pytest.approx(expect, rel=1e-12)


def test_merge_same_profile_noop(store):
    store.associate("a1", "c1")
    store.merge("c1", "c1")


def test_merge_requires_customer_ids(store):
    store.associate("a1", "c1")
    with pytest.raises(MergeError):
        store.merge("c1", "a1")
    with pytest.raises(MergeError):
        store.merge("a1", "c1")


def test_merge_multi_level(store):
    store.associate("a1", "c1")
    store.associate("a2", "c2")
    store.associate("a3", "c3")
    store.merge("c2", "c1")
    store.merge("c3", "c1")
    pid = store.query("c1", "2026-09-28T00:00:00Z").profile_id
    for ident in ("a1", "a2", "a3", "c2", "c3"):
        assert store.query(ident, "2026-09-28T00:00:00Z").profile_id == pid


# ---- 撤销关联 ----
def test_dissociate_pulls_all_events_back(store):
    store.associate("a1", "c1")
    store.ingest([
        ev("e1", "a1", KIND_ANONYMOUS, "purchase", "2026-09-20T00:00:00Z", "x"),
        ev("e2", "c1", KIND_CUSTOMER, "page_view", "2026-09-21T00:00:00Z", "x"),
    ])
    store.dissociate("a1")
    ra = store.query("a1", "2026-09-28T00:00:00Z")
    rc = store.query("c1", "2026-09-28T00:00:00Z")
    assert ra.profile_id != rc.profile_id
    assert ra.categories["x"].short == pytest.approx(decay(5, 8, 7), rel=1e-12)
    assert rc.categories["x"].short == pytest.approx(decay(1, 7, 7), rel=1e-12)


def test_dissociate_includes_events_reported_after_association(store):
    store.associate("a1", "c1")
    store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "purchase",
                     "2026-09-20T00:00:00Z", "x")])
    store.dissociate("a1")
    assert "x" in store.query("a1", "2026-09-28T00:00:00Z").categories
    assert store.query("c1", "2026-09-28T00:00:00Z").categories == {}


def test_dissociate_unassociated_errors(store):
    store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "page_view",
                     "2026-09-28T00:00:00Z", "x")])
    with pytest.raises(DissociationError):
        store.dissociate("a1")
    with pytest.raises(DissociationError):
        store.dissociate("never-seen")
    with pytest.raises(DissociationError):
        store.dissociate("c-not-anon")


def test_dissociate_then_reassociate(store):
    store.associate("a1", "c1")
    store.ingest([ev("e1", "a1", KIND_ANONYMOUS, "purchase",
                     "2026-09-20T00:00:00Z", "x")])
    store.dissociate("a1")
    store.associate("a1", "c2")
    r = store.query("c2", "2026-09-28T00:00:00Z")
    assert r.categories["x"].short == pytest.approx(decay(5, 8, 7), rel=1e-12)
    assert store.query("c1", "2026-09-28T00:00:00Z").categories == {}


# ---- 重启持久化 ----
def test_restart_preserves_state():
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    with InterestProfileStore(path) as s:
        s.associate("a1", "c1")
        s.ingest([ev("e1", "a1", KIND_ANONYMOUS, "purchase",
                     "2026-09-20T00:00:00Z", "x")])
        s.merge("c2", "c1")
    with InterestProfileStore(path) as s:
        r = s.query("a1", "2026-09-28T00:00:00Z")
        assert r.categories["x"].short == pytest.approx(decay(5, 8, 7), rel=1e-12)
        assert s.query("c2", "2026-09-28T00:00:00Z").profile_id == r.profile_id
        assert s.ingest([ev("e1", "a1", KIND_ANONYMOUS, "purchase",
                            "2026-09-20T00:00:00Z", "x")]) == 0

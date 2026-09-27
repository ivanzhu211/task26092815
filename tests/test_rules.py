"""业务规则单元测试。"""

import math

import pytest

from interest_profile import (
    AlreadyLinkedError,
    Event,
    IdentityKindError,
    InterestProfileService,
    NotLinkedError,
    QueryTimeError,
    UnknownIdentityError,
)

T0 = "2026-09-01T00:00:00+08:00"
DAY = 86400.0


def at(days_after_t0):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8)))
    return (base + timedelta(days=days_after_t0)).isoformat()


@pytest.fixture
def svc(tmp_path):
    service = InterestProfileService(str(tmp_path / "p.db"))
    yield service
    service.close()


def ev(event_id, identity, kind, etype, day, category="cat"):
    return Event(event_id, identity, kind, etype, at(day), category)


def scores_of(profile):
    return {s.category: s for s in profile.scores}


def test_weights_and_decay(svc):
    svc.ingest(
        [
            ev("e1", "c1", "customer", "purchase", 0),
            ev("e2", "c1", "customer", "page_view", 0),
            ev("e3", "c1", "customer", "email_open", 0),
        ]
    )
    s = scores_of(svc.query("c1", at(7)))["cat"]
    # 7 天后：短期半衰期恰好过一个，长期过 7/90
    total = 5.0 + 1.0 + 0.5
    assert s.short_score == pytest.approx(total * 0.5, rel=1e-12)
    assert s.long_score == pytest.approx(total * 0.5 ** (7 / 90), rel=1e-12)
    assert s.fused_score == pytest.approx(
        0.7 * s.short_score + 0.3 * s.long_score, rel=1e-12
    )


def test_unweighted_type_and_missing_category(svc):
    svc.ingest(
        [
            ev("e1", "c1", "customer", "click", 0, "cat"),
            ev("e2", "c1", "customer", "purchase", 0, None),
            ev("e3", "c1", "customer", "purchase", 0, "   "),
            ev("e4", "c1", "customer", "purchase", 0, "cat"),
        ]
    )
    profile = svc.query("c1", at(1))
    assert [s.category for s in profile.scores] == ["cat"]
    assert scores_of(profile)["cat"].short_score == pytest.approx(
        5.0 * 0.5 ** (1 / 7), rel=1e-12
    )


def test_category_normalized(svc):
    svc.ingest(
        [
            ev("e1", "c1", "customer", "purchase", 0, "  Books "),
            ev("e2", "c1", "customer", "purchase", 0, "BOOKS"),
        ]
    )
    profile = svc.query("c1", at(0))
    assert [s.category for s in profile.scores] == ["books"]
    assert scores_of(profile)["books"].short_score == pytest.approx(10.0, rel=1e-12)


def test_duplicate_event_id_first_wins(svc):
    svc.ingest([ev("e1", "a1", "anonymous", "purchase", 0, "x")])
    # 同一事件 ID 换标识重复上报，只算第一次
    svc.ingest([ev("e1", "c1", "customer", "purchase", 0, "y")])
    assert svc.query("a1", at(0)).scores[0].category == "x"
    with pytest.raises(UnknownIdentityError):
        svc.query("c1", at(0))


def test_out_of_order_events(svc):
    svc.ingest([ev("e2", "c1", "customer", "purchase", 5)])
    svc.ingest([ev("e1", "c1", "customer", "purchase", 0)])
    s = scores_of(svc.query("c1", at(10)))["cat"]
    expect = 5.0 * 0.5 ** (10 / 7) + 5.0 * 0.5 ** (5 / 7)
    assert s.short_score == pytest.approx(expect, rel=1e-12)


def test_link_moves_past_and_future_events(svc):
    svc.ingest([ev("e1", "a1", "anonymous", "purchase", 0, "old")])
    svc.link("a1", "c1")
    svc.ingest([ev("e2", "a1", "anonymous", "purchase", 1, "new")])
    profile = svc.query("c1", at(2))
    assert set(scores_of(profile)) == {"old", "new"}
    assert set(profile.identities) == {"a1", "c1"}


def test_link_same_profile_noop(svc):
    svc.link("a1", "c1")
    svc.link("a1", "c1")  # 同一画像，无变化
    profile = svc.query("c1", at(0))
    assert set(profile.identities) == {"a1", "c1"}


def test_link_to_other_profile_errors(svc):
    svc.link("a1", "c1")
    svc.ingest([ev("e1", "c2", "customer", "purchase", 0)])
    with pytest.raises(AlreadyLinkedError):
        svc.link("a1", "c2")


def test_link_kind_check(svc):
    svc.ingest([ev("e1", "c1", "customer", "purchase", 0)])
    svc.ingest([ev("e2", "a2", "anonymous", "purchase", 0)])
    with pytest.raises(IdentityKindError):
        svc.link("c1", "c2")  # 客户ID不能作为被关联的匿名ID
    with pytest.raises(IdentityKindError):
        svc.link("a1", "a2")  # 关联目标必须是客户ID


def test_merge_combines_profiles(svc):
    svc.ingest([ev("e1", "cb", "customer", "purchase", 0, "x")])
    svc.link("a1", "cb")
    svc.ingest([ev("e2", "a1", "anonymous", "page_view", 0, "y")])
    svc.ingest([ev("e3", "ca", "customer", "email_open", 0, "z")])
    svc.merge("cb", "ca")
    profile = svc.query("ca", at(0))
    assert set(scores_of(profile)) == {"x", "y", "z"}
    assert set(profile.identities) == {"ca", "cb", "a1"}
    # 用 B 或挂在 B 上的匿名ID查询，都得到 A 的画像
    assert svc.query("cb", at(0)).profile_id == profile.profile_id
    assert svc.query("a1", at(0)).profile_id == profile.profile_id


def test_merge_same_profile_noop(svc):
    svc.link("a1", "c1")
    svc.merge("c1", "c1")
    svc.ingest([ev("e1", "c2", "customer", "purchase", 0)])
    svc.merge("c2", "c1")
    svc.merge("c2", "c1")  # 已属同一画像
    assert set(svc.query("c1", at(0)).identities) == {"a1", "c1", "c2"}


def test_merge_anonymous_rejected(svc):
    svc.ingest([ev("e1", "a1", "anonymous", "purchase", 0)])
    with pytest.raises(IdentityKindError):
        svc.merge("a1", "c1")
    with pytest.raises(IdentityKindError):
        svc.merge("c1", "a1")


def test_unlink_takes_all_events_back(svc):
    svc.ingest([ev("e1", "a1", "anonymous", "purchase", 0, "before")])
    svc.link("a1", "c1")
    svc.ingest([ev("e2", "a1", "anonymous", "purchase", 1, "after")])
    svc.ingest([ev("e3", "c1", "customer", "purchase", 1, "cust")])
    svc.unlink("a1")
    anon_profile = svc.query("a1", at(2))
    assert set(scores_of(anon_profile)) == {"before", "after"}
    assert anon_profile.identities == ("a1",)
    cust_profile = svc.query("c1", at(2))
    assert set(scores_of(cust_profile)) == {"cust"}
    assert cust_profile.identities == ("c1",)


def test_unlink_then_relink_elsewhere(svc):
    svc.link("a1", "c1")
    svc.unlink("a1")
    svc.link("a1", "c2")
    assert svc.query("a1", at(0)).profile_id == svc.query("c2", at(0)).profile_id


def test_unlink_unlinked_errors(svc):
    svc.ingest([ev("e1", "a1", "anonymous", "purchase", 0)])
    with pytest.raises(NotLinkedError):
        svc.unlink("a1")
    with pytest.raises(NotLinkedError):
        svc.unlink("never-seen")
    svc.ingest([ev("e2", "c1", "customer", "purchase", 0)])
    with pytest.raises(IdentityKindError):
        svc.unlink("c1")


def test_query_time_before_event_errors(svc):
    svc.ingest([ev("e1", "c1", "customer", "purchase", 5)])
    with pytest.raises(QueryTimeError):
        svc.query("c1", at(4))
    svc.query("c1", at(5))  # 等于事件时间，允许


def test_query_unknown_identity(svc):
    with pytest.raises(UnknownIdentityError):
        svc.query("ghost", at(0))


def test_restart_persistence(tmp_path):
    path = str(tmp_path / "p.db")
    svc1 = InterestProfileService(path)
    svc1.ingest([ev("e1", "a1", "anonymous", "purchase", 0)])
    svc1.link("a1", "c1")
    svc1.close()
    svc2 = InterestProfileService(path)
    profile = svc2.query("c1", at(1))
    assert set(profile.identities) == {"a1", "c1"}
    assert scores_of(profile)["cat"].short_score == pytest.approx(
        5.0 * 0.5 ** (1 / 7), rel=1e-12
    )
    # 重启后重复事件仍被去重
    assert svc2.ingest([ev("e1", "a1", "anonymous", "purchase", 0)]) == 0
    svc2.close()


def test_batch_size_limit(svc):
    events = [ev(f"e{i}", "c1", "customer", "purchase", 0) for i in range(5001)]
    with pytest.raises(ValueError):
        svc.ingest(events)


def test_old_backfilled_events(svc):
    # 2005 年的历史订单补录
    svc.ingest(
        [
            Event(
                "old-1", "c1", "customer", "purchase",
                "2005-06-15T12:00:00+00:00", "legacy",
            )
        ]
    )
    svc.ingest([ev("e1", "c1", "customer", "purchase", 0, "recent")])
    profile = svc.query("c1", at(1))
    got = scores_of(profile)
    assert set(got) == {"legacy", "recent"}
    # 2005 年的事件到现在衰减到远低于 1e-12，贡献可忽略但类别保留
    assert got["legacy"].short_score >= 0.0

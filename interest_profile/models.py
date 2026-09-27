"""数据类型定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: 匿名标识
KIND_ANONYMOUS = "anonymous"
#: 客户标识
KIND_CUSTOMER = "customer"


@dataclass(frozen=True)
class Event:
    """一条行为事件。

    occurred_at: 带时区的 ISO 8601 时间字符串。
    category: 可为 None 或空白，表示缺失。
    """

    event_id: str
    identity: str
    identity_kind: str
    event_type: str
    occurred_at: str
    category: Optional[str] = None


@dataclass(frozen=True)
class CategoryScore:
    """画像内单个类别的三个分数。"""

    category: str
    short_score: float
    long_score: float
    fused_score: float


@dataclass(frozen=True)
class Profile:
    """一个画像的查询结果。"""

    profile_id: int
    identities: tuple
    scores: tuple  # tuple[CategoryScore, ...]，按类别名排序

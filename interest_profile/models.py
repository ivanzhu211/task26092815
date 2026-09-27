"""类型定义：事件、画像结果、异常。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


KIND_ANONYMOUS = "anonymous"
KIND_CUSTOMER = "customer"

WEIGHTS = {
    "purchase": 5.0,
    "page_view": 1.0,
    "email_open": 0.5,
}

HALF_LIFE_SHORT_DAYS = 7.0
HALF_LIFE_LONG_DAYS = 90.0
FUSED_SHORT_RATIO = 0.7
FUSED_LONG_RATIO = 0.3


@dataclass(frozen=True)
class Event:
    event_id: str
    identity: str
    identity_kind: str  # KIND_ANONYMOUS or KIND_CUSTOMER
    event_type: str
    timestamp: datetime  # 带时区
    category: Optional[str] = None


@dataclass(frozen=True)
class CategoryScore:
    short: float
    long: float
    fused: float


@dataclass(frozen=True)
class ProfileResult:
    profile_id: int
    categories: dict = field(default_factory=dict)  # str -> CategoryScore


class ProfileError(Exception):
    """本库所有业务错误的基类。"""


class UnknownIdentityError(ProfileError):
    pass


class AssociationError(ProfileError):
    pass


class DissociationError(ProfileError):
    pass


class MergeError(ProfileError):
    pass


class QueryTimeError(ProfileError):
    pass


def parse_timestamp(value) -> datetime:
    """把带时区的 ISO 8601 字符串或 datetime 解析为带时区的 datetime。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"无法解析的时间: {value!r}")
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value!r}")
    return dt


def normalize_category(category):
    """去首尾空白并转小写；为空或缺失返回 None。"""
    if category is None:
        return None
    text = str(category).strip().lower()
    return text or None

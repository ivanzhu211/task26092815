"""权重与衰减计算。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

#: 事件类型权重，未列出的类型不计入兴趣
EVENT_WEIGHTS = {
    "purchase": 5.0,
    "page_view": 1.0,
    "email_open": 0.5,
}

#: 短期半衰期（秒）：7 天
SHORT_HALF_LIFE = 7.0 * 86400.0
#: 长期半衰期（秒）：90 天
LONG_HALF_LIFE = 90.0 * 86400.0

#: 融合分系数
SHORT_FUSION = 0.7
LONG_FUSION = 0.3


def normalize_category(category: Optional[str]) -> Optional[str]:
    """类别去首尾空白并转小写；为空或缺失返回 None。"""
    if category is None:
        return None
    normalized = category.strip().lower()
    return normalized or None


def event_weight(event_type: str) -> float:
    """事件类型对应权重，不计入的类型返回 0。"""
    return EVENT_WEIGHTS.get(event_type, 0.0)


def decay(dt_seconds: float, half_life: float) -> float:
    """0.5 的 (dt/半衰期) 次方。"""
    return 0.5 ** (dt_seconds / half_life)


def parse_instant(iso_text: str) -> float:
    """解析带时区的 ISO 8601 时间为 Unix 秒。"""
    text = iso_text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()

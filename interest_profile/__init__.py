"""客户兴趣画像库：供上层客户数据平台直接调用。"""
from interest_profile.models import (
    CategoryScore,
    Event,
    ProfileResult,
    ProfileError,
    UnknownIdentityError,
    AssociationError,
    DissociationError,
    MergeError,
    QueryTimeError,
    KIND_ANONYMOUS,
    KIND_CUSTOMER,
)
from interest_profile.core import InterestProfileStore

__all__ = [
    "CategoryScore",
    "Event",
    "ProfileResult",
    "ProfileError",
    "UnknownIdentityError",
    "AssociationError",
    "DissociationError",
    "MergeError",
    "QueryTimeError",
    "KIND_ANONYMOUS",
    "KIND_CUSTOMER",
    "InterestProfileStore",
]

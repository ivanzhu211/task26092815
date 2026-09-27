"""客户兴趣画像模块。"""

from .core import InterestProfileService
from .errors import (
    AlreadyLinkedError,
    IdentityKindError,
    NotLinkedError,
    ProfileError,
    QueryTimeError,
    UnknownIdentityError,
)
from .models import KIND_ANONYMOUS, KIND_CUSTOMER, CategoryScore, Event, Profile

__all__ = [
    "InterestProfileService",
    "Event",
    "Profile",
    "CategoryScore",
    "KIND_ANONYMOUS",
    "KIND_CUSTOMER",
    "ProfileError",
    "IdentityKindError",
    "AlreadyLinkedError",
    "NotLinkedError",
    "QueryTimeError",
    "UnknownIdentityError",
]

"""异常定义。"""


class ProfileError(Exception):
    """画像模块所有业务异常的基类。"""


class IdentityKindError(ProfileError):
    """标识类型不满足操作要求（如对匿名ID执行合并）。"""


class AlreadyLinkedError(ProfileError):
    """匿名ID已关联到其他画像，重复关联报错。"""


class NotLinkedError(ProfileError):
    """对未关联的标识执行撤销关联。"""


class QueryTimeError(ProfileError):
    """查询时刻早于画像中事件时间。"""


class UnknownIdentityError(ProfileError):
    """标识不存在。"""

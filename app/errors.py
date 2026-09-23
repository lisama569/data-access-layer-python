"""对外可解释的错误类型体系。

所有底层存储异常都会被转译成本模块中的某个子类，绝不原样透出。
- DALError: 数据访问层错误的统一基类
- ConflictError: 请求与当前数据状态冲突（基类）
- VersionConflictError: 乐观锁版本号冲突
- NotFoundError: 目标资源不存在 / 已被删除
- InvalidCursorError: 分页游标非法
- StorageError: 存储层故障（已剥离底层细节）
"""

from __future__ import annotations


class DALError(Exception):
    """数据访问层对外错误基类。"""


class NotFoundError(DALError):
    """资源不存在。"""


class ConflictError(DALError):
    """请求与当前状态冲突（冲突原因可通过 reason 区分）。"""

    reason: str = "conflict"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message or self.reason)
        if reason is not None:
            self.reason = reason


class VersionConflictError(ConflictError):
    """乐观锁：客户端携带的版本号与数据库当前版本不一致。"""

    reason = "version_conflict"

    def __init__(
        self,
        message: str = "",
        *,
        resource_id: str = "",
        expected_version: int = -1,
        actual_version: int = -1,
    ) -> None:
        super().__init__(message or self.reason)
        self.resource_id = resource_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class InvalidCursorError(DALError):
    """游标非法（格式错误 / 字段缺失 / 解码失败 / 排序键不可比）。"""


class StorageError(DALError):
    """底层存储故障（对外不携带驱动原始信息）。"""

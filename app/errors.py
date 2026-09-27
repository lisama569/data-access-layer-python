"""对外可解释的错误类型。

所有数据访问层对外抛出的错误都继承自 DataAccessError，
底层存储异常一律不直接透出。
"""
from __future__ import annotations


class DataAccessError(Exception):
    """数据访问层对外错误基类。"""

    code: str = "data_access_error"

    def __init__(self, message: str = "", *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class ResourceNotFoundError(DataAccessError):
    code = "resource_not_found"


class VersionConflictError(DataAccessError):
    """乐观锁版本冲突。"""

    code = "version_conflict"

    def __init__(
        self,
        message: str = "",
        *,
        resource_id: str = "",
        expected_version: int = -1,
        current_version: int = -1,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.resource_id = resource_id
        self.expected_version = expected_version
        self.current_version = current_version


class ResourceStateError(DataAccessError):
    """资源当前状态不允许该操作（如删除已删除的资源、恢复正常资源）。"""

    code = "resource_state_conflict"

    def __init__(
        self,
        message: str = "",
        *,
        resource_id: str = "",
        current_state: str = "",
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.resource_id = resource_id
        self.current_state = current_state


class UniqueConstraintError(DataAccessError):
    code = "unique_constraint_violation"


class InvalidCursorError(DataAccessError):
    code = "invalid_cursor"


class TransactionError(DataAccessError):
    code = "transaction_failed"


class StorageError(DataAccessError):
    """底层存储异常的统一包装，不暴露原始异常类型。"""

    code = "storage_error"


class InvalidInputError(DataAccessError):
    code = "invalid_input"

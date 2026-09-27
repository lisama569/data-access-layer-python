"""数据模型与查询参数（骨架：仅类型定义）。"""
from __future__ import annotations

from dataclasses import dataclass, field

STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"
VALID_STATUSES = (STATUS_ACTIVE, STATUS_DELETED)


@dataclass(slots=True)
class Resource:
    """资源实体。"""

    id: str
    name: str
    payload: str = ""
    version: int = 1
    created_at: int = 0
    updated_at: int = 0
    status: str = STATUS_ACTIVE
    deleted_at: int | None = None


@dataclass(slots=True)
class ResourceCreate:
    name: str
    payload: str = ""
    id: str | None = None


@dataclass(slots=True)
class ResourceUpdate:
    """基于版本号的乐观锁更新请求。"""

    id: str
    expected_version: int
    name: str | None = None
    payload: str | None = None


@dataclass(slots=True)
class ResourceStateChange:
    """基于版本号的删除 / 恢复请求。"""

    id: str
    expected_version: int


@dataclass(slots=True)
class ResourceQuery:
    name_contains: str | None = None
    status: str = STATUS_ACTIVE
    page_size: int = 20
    cursor: str | None = None


@dataclass(slots=True)
class Page:
    """游标分页结果。"""

    items: list[Resource] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


@dataclass(slots=True)
class PurgeResult:
    """单批（或一次）保留期清理的可解释结果。"""

    purged: int = 0
    scanned: int = 0
    has_more: bool = False
    batches: int = 0

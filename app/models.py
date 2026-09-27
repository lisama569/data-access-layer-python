"""数据模型与查询参数。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ResourceStatus(StrEnum):
    """资源生命周期状态。"""

    ACTIVE = "active"      # 正常，默认单查/列表可见
    DELETED = "deleted"    # 在回收站，默认读取不可见，可恢复/可清理
    ALL = "all"            # 仅用于列表筛选，非资源真实状态


@dataclass(slots=True)
class Resource:
    """资源实体。"""

    id: str
    name: str
    payload: str = ""
    version: int = 1
    created_at: int = 0
    updated_at: int = 0
    status: ResourceStatus = ResourceStatus.ACTIVE
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
class ResourceDelete:
    """基于版本号的乐观锁删除（移入回收站）请求。"""

    id: str
    expected_version: int


@dataclass(slots=True)
class ResourceRestore:
    """基于版本号的乐观锁恢复请求。"""

    id: str
    expected_version: int


@dataclass(slots=True)
class ResourceQuery:
    name_contains: str | None = None
    page_size: int = 20
    cursor: str | None = None
    status: ResourceStatus = ResourceStatus.ACTIVE


@dataclass(slots=True)
class Page:
    """游标分页结果。"""

    items: list[Resource] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


@dataclass(slots=True)
class PurgeResult:
    """回收站超期清理的可解释结果（不含任何底层存储细节）。"""

    purged_count: int = 0
    batch_count: int = 0
    cutoff_ms: int = 0
    retention_ms: int = 0
    has_more: bool = False

"""领域模型：资源(Resource)与分页容器(Page)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class Resource:
    """资源实体。

    version 从 1 开始，每次成功更新自增 1；删除后版本号不复用。
    created_at / updated_at 为 UTC ISO-8601 字符串（微秒精度）。
    """

    id: str
    name: str
    status: str
    version: int
    created_at: str
    updated_at: str


@dataclass(slots=True)
class Page(Generic[T]):
    """游标分页结果。

    - items: 本页记录（按稳定排序键升序）
    - next_cursor: 下一页游标；为空串表示已到末页（终止语义）
    - has_more: 是否还有后续记录
    """

    items: list[T] = field(default_factory=list)
    next_cursor: str = ""
    has_more: bool = False

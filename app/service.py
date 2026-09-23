"""数据访问服务：事务边界 + 仓储的编排入口。

- 写操作（新增 / 更新 / 批量）都在一个 ``transactional`` 边界内完成，
  多步写入要么全部提交、要么整体回滚。
- 读操作（单查 / 分页）使用短事务，避免长连接持锁。
- ID 与毫秒时间戳由本层生成，保证排序键来源一致。
- 关键路径打印版本号、事务结果与游标位置，便于排查并发与分页问题。
"""
from __future__ import annotations

import logging
import uuid
from time import time_ns

from app.connection import Database
from app.errors import InvalidInputError, ResourceNotFoundError
from app.models import Page, Resource, ResourceCreate, ResourceQuery, ResourceUpdate
from app.repository import MAX_PAGE_SIZE, ResourceRepository
from app.transaction import transactional

logger = logging.getLogger("app.dal")


def _now_ms() -> int:
    return time_ns() // 1_000_000


class ResourceService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- 写 ----------

    def create_resource(self, data: ResourceCreate) -> Resource:
        self._validate_name(data.name)
        resource_id = data.id or uuid.uuid4().hex
        now_ms = _now_ms()
        try:
            with transactional(self.db) as conn:
                repo = ResourceRepository(conn)
                resource = repo.insert(data, now_ms=now_ms, resource_id=resource_id)
                logger.info(
                    "[TX commit] create id=%s version=%d ts=%d",
                    resource.id, resource.version, now_ms,
                )
            return resource
        except Exception:
            logger.info("[TX rollback] create id=%s ts=%d", resource_id, now_ms)
            raise

    def update_resource(self, change: ResourceUpdate) -> Resource:
        if not change.id:
            raise InvalidInputError("资源 id 不能为空")
        if change.expected_version < 1:
            raise InvalidInputError("expected_version 必须为正整数")
        if change.name is not None:
            self._validate_name(change.name)
        now_ms = _now_ms()
        try:
            with transactional(self.db) as conn:
                repo = ResourceRepository(conn)
                if repo.get(change.id) is None:
                    raise ResourceNotFoundError(f"资源不存在: id={change.id}")
                updated = repo.update_optimistic(change, now_ms=now_ms)
                logger.info(
                    "[TX commit] update id=%s version %d->%d",
                    updated.id, change.expected_version, updated.version,
                )
            return updated
        except Exception:
            logger.info(
                "[TX rollback] update id=%s expected_version=%d ts=%d",
                change.id, change.expected_version, now_ms,
            )
            raise

    def batch_create(self, items: list[ResourceCreate]) -> int:
        """同一事务内顺序新增，全部成功才提交，返回写入条数。"""
        if not items:
            raise InvalidInputError("批量写入列表不能为空")
        now_ms = _now_ms()
        count = 0
        try:
            with transactional(self.db) as conn:
                repo = ResourceRepository(conn)
                for idx, item in enumerate(items):
                    self._validate_name(item.name)
                    repo.insert(item, now_ms=now_ms + idx, resource_id=uuid.uuid4().hex)
                    count += 1
            logger.info("[TX commit] batch_create count=%d", count)
            return count
        except Exception:
            logger.info(
                "[TX rollback] batch_create count_before_failure=%d", count
            )
            raise

    # ---------- 读 ----------

    def get_resource(self, resource_id: str) -> Resource:
        with transactional(self.db) as conn:
            resource = ResourceRepository(conn).get(resource_id)
        if resource is None:
            raise ResourceNotFoundError(f"资源不存在: id={resource_id}")
        return resource

    def list_resources(self, query: ResourceQuery) -> Page:
        if query.page_size < 1 or query.page_size > MAX_PAGE_SIZE:
            raise InvalidInputError(
                f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间"
            )
        with transactional(self.db) as conn:
            page = ResourceRepository(conn).query_page(query)
        logger.info(
            "[PAGE] returned=%d has_more=%s cursor_next=%s",
            len(page.items), page.has_more, page.next_cursor,
        )
        return page

    # ---------- 校验 ----------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise InvalidInputError("name 不能为空")

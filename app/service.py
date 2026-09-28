"""数据访问服务：事务边界 + 仓储的编排入口。

- 写操作（新增 / 更新 / 删除 / 恢复 / 批量）都在一个 ``transactional`` 边界内
  完成，多步写入要么全部提交、要么整体回滚。
- 删除是软删除（进回收站），可带版本号恢复；回收站清理按保留期分批进行，
  每批独立短事务。
- 读操作（单查 / 分页）使用短事务，避免长连接持锁；默认视角不含回收站。
- ID 与毫秒时间戳由本层生成（时钟可注入），保证排序键与保留期判定来源一致。
- 关键路径打印版本号、事务结果与游标位置，便于排查并发与分页问题。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from time import time_ns

from app.connection import Database
from app.errors import InvalidInputError, ResourceNotFoundError
from app.models import (
    STATUS_ACTIVE,
    VALID_STATUSES,
    Page,
    PurgeResult,
    Resource,
    ResourceCreate,
    ResourceQuery,
    ResourceStateChange,
    ResourceUpdate,
)
from app.repository import MAX_PAGE_SIZE, ResourceRepository
from app.transaction import transactional

logger = logging.getLogger("app.dal")

DEFAULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000  # 默认回收站保留 7 天
DEFAULT_PURGE_BATCH = 500


def _now_ms() -> int:
    return time_ns() // 1_000_000


class ResourceService:
    def __init__(
        self,
        db: Database,
        *,
        default_retention_ms: int = DEFAULT_RETENTION_MS,
        purge_batch_size: int = DEFAULT_PURGE_BATCH,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self.db = db
        self.default_retention_ms = default_retention_ms
        self.purge_batch_size = purge_batch_size
        # 单一时间来源：删除/恢复写 deleted_at 与清理判断到期用同一个时钟，
        # 默认墙钟；测试可注入确定性时钟。
        self._clock = clock

    # ---------- 写 ----------

    def create_resource(self, data: ResourceCreate) -> Resource:
        self._validate_name(data.name)
        resource_id = data.id or uuid.uuid4().hex
        now_ms = self._clock()
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
        now_ms = self._clock()
        try:
            with transactional(self.db) as conn:
                # 仓储层统一判定：物理不存在或已在回收站都视为 not_found
                updated = ResourceRepository(conn).update_optimistic(
                    change, now_ms=now_ms
                )
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
        now_ms = self._clock()
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

    # ---------- 生命周期：删除 / 恢复 / 清理 ----------

    def delete_resource(self, change: ResourceStateChange) -> Resource:
        self._validate_state_change(change)
        now_ms = self._clock()
        try:
            with transactional(self.db) as conn:
                resource = ResourceRepository(conn).soft_delete(change, now_ms=now_ms)
                logger.info(
                    "[TX commit] delete id=%s version %d->%d deleted_at=%d",
                    resource.id, change.expected_version, resource.version, now_ms,
                )
            return resource
        except Exception:
            logger.info(
                "[TX rollback] delete id=%s expected_version=%d",
                change.id, change.expected_version,
            )
            raise

    def restore_resource(self, change: ResourceStateChange) -> Resource:
        self._validate_state_change(change)
        now_ms = self._clock()
        try:
            with transactional(self.db) as conn:
                resource = ResourceRepository(conn).restore(change, now_ms=now_ms)
                logger.info(
                    "[TX commit] restore id=%s version %d->%d",
                    resource.id, change.expected_version, resource.version,
                )
            return resource
        except Exception:
            logger.info(
                "[TX rollback] restore id=%s expected_version=%d",
                change.id, change.expected_version,
            )
            raise

    def purge_deleted(
        self,
        *,
        retention_ms: int | None = None,
        batch_size: int | None = None,
        max_batches: int | None = None,
        now_ms: int | None = None,
    ) -> PurgeResult:
        """按保留期分批物理清除回收站资源。

        - ``retention_ms``：回收站保留时长，缺省用服务级默认值，必须为非负整数；
          保留期从软删除写入的 ``deleted_at`` 起算，**严格超过**
          （``deleted_at + retention_ms < now``）才到期，卡在边界不清；
        - 每批在自己的短事务里提交，批间不持锁，避免长时间阻塞正常读写；
        - 中途失败重试是幂等的：已提交的批不会被重复清除，到期判定始终
          基于 ``deleted_at + retention_ms`` 与当前时间重新计算，保留期内
          （含边界）的数据不会被误伤；
        - 返回 :class:`PurgeResult` 解释本次清了多少、扫描了多少、**清理后**
          是否还有到期积压（最后一批恰好清空时 ``has_more`` 必为 False）。
        """
        retention = self.default_retention_ms if retention_ms is None else retention_ms
        size = self.purge_batch_size if batch_size is None else batch_size
        if retention < 0:
            raise InvalidInputError("retention_ms 必须为非负整数")
        if size < 1:
            raise InvalidInputError("batch_size 必须为正整数")
        if max_batches is not None and max_batches < 1:
            raise InvalidInputError("max_batches 必须为正整数")

        clock = self._clock() if now_ms is None else now_ms
        total_purged = 0
        total_scanned = 0
        batches = 0
        has_more = True
        while has_more:
            if max_batches is not None and batches >= max_batches:
                break
            with transactional(self.db) as conn:
                purged, scanned, has_more = ResourceRepository(conn).purge_expired(
                    now_ms=clock, retention_ms=retention, batch_size=size
                )
            batches += 1
            total_purged += purged
            total_scanned += scanned
            logger.info(
                "[PURGE] batch=%d purged=%d scanned=%d has_more=%s",
                batches, purged, scanned, has_more,
            )
        result = PurgeResult(
            purged=total_purged,
            scanned=total_scanned,
            has_more=has_more,
            batches=batches,
        )
        logger.info(
            "[PURGE] done purged=%d scanned=%d batches=%d has_more=%s",
            result.purged, result.scanned, result.batches, result.has_more,
        )
        return result

    @staticmethod
    def _validate_state_change(change: ResourceStateChange) -> None:
        if not change.id:
            raise InvalidInputError("资源 id 不能为空")
        if change.expected_version < 1:
            raise InvalidInputError("expected_version 必须为正整数")

    # ---------- 读 ----------

    def get_resource(self, resource_id: str, *, include_deleted: bool = False) -> Resource:
        """默认只返回正常资源；回收站资源在默认视角下等同于不存在。"""
        with transactional(self.db) as conn:
            resource = ResourceRepository(conn).get(resource_id)
        if resource is None or (
            resource.status != STATUS_ACTIVE and not include_deleted
        ):
            raise ResourceNotFoundError(f"资源不存在: id={resource_id}")
        return resource

    def list_resources(self, query: ResourceQuery) -> Page:
        if query.page_size < 1 or query.page_size > MAX_PAGE_SIZE:
            raise InvalidInputError(
                f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间"
            )
        if query.status not in (*VALID_STATUSES, "all"):
            raise InvalidInputError(
                f"status 必须是 {VALID_STATUSES + ('all',)} 之一"
            )
        with transactional(self.db) as conn:
            page = ResourceRepository(conn).query_page(query)
        logger.info(
            "[PAGE] status=%s returned=%d has_more=%s cursor_next=%s",
            query.status, len(page.items), page.has_more, page.next_cursor,
        )
        return page

    # ---------- 校验 ----------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise InvalidInputError("name 不能为空")

"""数据访问服务：事务边界 + 仓储的编排入口。

- 写操作（新增 / 更新 / 软删除 / 恢复 / 批量 / 清理）都在事务边界内完成，
  多步写入要么全部提交、要么整体回滚。
- 回收站超期清理按批次推进：每批一个独立短事务，避免长时间持锁阻塞正常读写。
- 读操作（单查 / 分页）使用短事务，避免长连接持锁。
- ID 与毫秒时间戳由本层生成，保证排序键来源一致；时钟可注入，便于测试保留期。
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
    Page,
    PurgeResult,
    Resource,
    ResourceCreate,
    ResourceDelete,
    ResourceQuery,
    ResourceRestore,
    ResourceStatus,
    ResourceUpdate,
)
from app.repository import MAX_PAGE_SIZE, ResourceRepository
from app.transaction import transactional

logger = logging.getLogger("app.dal")

# 默认回收站保留期 7 天；默认每批物理删除 100 行
DEFAULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000
DEFAULT_PURGE_BATCH_SIZE = 100


def _now_ms() -> int:
    return time_ns() // 1_000_000


class ResourceService:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], int] = _now_ms,
        default_retention_ms: int = DEFAULT_RETENTION_MS,
    ) -> None:
        self.db = db
        self._clock = clock
        self._default_retention_ms = default_retention_ms

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
        self._validate_version(change.expected_version)
        if change.name is not None:
            self._validate_name(change.name)
        now_ms = self._clock()
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

    def delete_resource(self, change: ResourceDelete) -> Resource:
        """软删除（移入回收站）：必须携带当前版本号。"""
        if not change.id:
            raise InvalidInputError("资源 id 不能为空")
        self._validate_version(change.expected_version)
        now_ms = self._clock()
        try:
            with transactional(self.db) as conn:
                repo = ResourceRepository(conn)
                if repo.get(change.id) is None:
                    raise ResourceNotFoundError(f"资源不存在: id={change.id}")
                deleted = repo.delete_soft(change, now_ms=now_ms)
                logger.info(
                    "[TX commit] delete id=%s version %d->%d deleted_at=%d",
                    deleted.id, change.expected_version, deleted.version, now_ms,
                )
            return deleted
        except Exception:
            logger.info(
                "[TX rollback] delete id=%s expected_version=%d ts=%d",
                change.id, change.expected_version, now_ms,
            )
            raise

    def restore_resource(self, change: ResourceRestore) -> Resource:
        """从回收站恢复：必须携带当前版本号，版本号继续向前。"""
        if not change.id:
            raise InvalidInputError("资源 id 不能为空")
        self._validate_version(change.expected_version)
        now_ms = self._clock()
        try:
            with transactional(self.db) as conn:
                repo = ResourceRepository(conn)
                if repo.get(change.id) is None:
                    raise ResourceNotFoundError(f"资源不存在: id={change.id}")
                restored = repo.restore(change, now_ms=now_ms)
                logger.info(
                    "[TX commit] restore id=%s version %d->%d",
                    restored.id, change.expected_version, restored.version,
                )
            return restored
        except Exception:
            logger.info(
                "[TX rollback] restore id=%s expected_version=%d ts=%d",
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

    # ---------- 读 ----------

    def get_resource(
        self, resource_id: str, *, include_deleted: bool = False
    ) -> Resource:
        """单条读取。默认把回收站中的资源视为不存在。

        ``include_deleted=True`` 时也返回回收站中的记录（管理/恢复场景）。
        """
        with transactional(self.db) as conn:
            resource = ResourceRepository(conn).get(resource_id)
        if resource is None or (
            resource.status is ResourceStatus.DELETED and not include_deleted
        ):
            raise ResourceNotFoundError(f"资源不存在: id={resource_id}")
        return resource

    def list_resources(self, query: ResourceQuery) -> Page:
        if query.page_size < 1 or query.page_size > MAX_PAGE_SIZE:
            raise InvalidInputError(
                f"page_size 必须在 1..{MAX_PAGE_SIZE} 之间"
            )
        if not isinstance(query.status, ResourceStatus):
            raise InvalidInputError(
                "status 必须是 active / deleted / all 之一"
            )
        with transactional(self.db) as conn:
            page = ResourceRepository(conn).query_page(query)
        logger.info(
            "[PAGE] status=%s returned=%d has_more=%s cursor_next=%s",
            query.status.value, len(page.items), page.has_more, page.next_cursor,
        )
        return page

    # ---------- 回收站清理 ----------

    def purge_deleted(
        self,
        *,
        retention_ms: int | None = None,
        batch_size: int = DEFAULT_PURGE_BATCH_SIZE,
        now_ms: int | None = None,
    ) -> PurgeResult:
        """按保留期物理清除回收站记录，分批短事务推进。

        - 仅删除 ``deleted_at + retention_ms < 当前时间`` 的记录，
          保留期内（含恰好到点）的数据绝不误伤；
        - 每批一个独立事务：提交后即释放写锁，不会长时间阻塞正常读写；
        - 删除顺序固定、按截止时间过滤，中途失败重试结果一致，
          已清掉的记录不会被重复处理；
        - 结果只包含计数与截止时间等可解释信息，不暴露底层存储细节。
        """
        retention = (
            self._default_retention_ms if retention_ms is None else retention_ms
        )
        if retention < 0:
            raise InvalidInputError("retention_ms 不能为负数")
        if batch_size < 1:
            raise InvalidInputError("batch_size 必须为正整数")

        reference_ms = self._clock() if now_ms is None else now_ms
        cutoff_ms = reference_ms - retention
        total = 0
        batches = 0
        while True:
            with transactional(self.db) as conn:
                deleted, has_more = ResourceRepository(conn).purge_expired(
                    cutoff_ms=cutoff_ms, batch_size=batch_size
                )
            total += deleted
            batches += 1
            logger.info(
                "[PURGE] batch=%d deleted=%d total=%d has_more=%s cutoff=%d",
                batches, deleted, total, has_more, cutoff_ms,
            )
            if not has_more:
                break
        return PurgeResult(
            purged_count=total,
            batch_count=batches,
            cutoff_ms=cutoff_ms,
            retention_ms=retention,
            has_more=False,
        )

    # ---------- 校验 ----------

    @staticmethod
    def _validate_version(version: int) -> None:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise InvalidInputError("expected_version 必须为正整数")

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise InvalidInputError("name 不能为空")

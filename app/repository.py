"""资源仓储：所有 SQL 读写集中于此。

行映射、乐观锁更新与游标分页（keyset pagination）均在本文件实现。
"""
from __future__ import annotations

import sqlite3

from app.cursor import decode_cursor, encode_cursor
from app.errors import (
    InvalidStateError,
    ResourceNotFoundError,
    VersionConflictError,
)
from app.models import (
    STATUS_ACTIVE,
    STATUS_DELETED,
    Page,
    Resource,
    ResourceCreate,
    ResourceQuery,
    ResourceUpdate,
)

MAX_PAGE_SIZE = 100

_INSERT_SQL = (
    "INSERT INTO resources (id, name, payload, version, created_at, updated_at, "
    "status, deleted_at) "
    "VALUES (:id, :name, :payload, 1, :now, :now, 'active', NULL)"
)

_UPDATE_SQL = (
    "UPDATE resources SET name = :name, payload = :payload, "
    "version = version + 1, updated_at = :now "
    "WHERE id = :id AND version = :expected_version AND status = 'active'"
)

_SOFT_DELETE_SQL = (
    "UPDATE resources SET status = 'deleted', deleted_at = :now, "
    "updated_at = :now, version = version + 1 "
    "WHERE id = :id AND version = :expected_version AND status = 'active'"
)

_RESTORE_SQL = (
    "UPDATE resources SET status = 'active', deleted_at = NULL, "
    "updated_at = :now, version = version + 1 "
    "WHERE id = :id AND version = :expected_version AND status = 'deleted'"
)

_PURGE_SELECT_SQL = (
    "SELECT id FROM resources WHERE status = 'deleted' "
    # 严格边界：只有 deleted_at + retention_ms < now（即 deleted_at < cutoff）
    # 才到期；刚好卡在边界（deleted_at = cutoff）仍在保留期内，不能清。
    "AND deleted_at IS NOT NULL AND deleted_at < :cutoff "
    "ORDER BY deleted_at ASC, id ASC LIMIT :limit"
)

_PURGE_REMAINING_SQL = (
    "SELECT 1 FROM resources WHERE status = 'deleted' "
    "AND deleted_at IS NOT NULL AND deleted_at < :cutoff LIMIT 1"
)

_PURGE_DELETE_SQL = "DELETE FROM resources WHERE id IN ({ids})"

_SELECT_SQL = (
    "SELECT id, name, payload, version, created_at, updated_at, status, deleted_at "
    "FROM resources"
)


def _row_to_resource(row: sqlite3.Row) -> Resource:
    return Resource(
        id=row["id"],
        name=row["name"],
        payload=row["payload"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        deleted_at=row["deleted_at"],
    )


class ResourceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, data: ResourceCreate, *, now_ms: int, resource_id: str) -> Resource:
        params = {
            "id": resource_id,
            "name": data.name,
            "payload": data.payload,
            "now": now_ms,
        }
        self.conn.execute(_INSERT_SQL, params)
        row = self.conn.execute(
            _SELECT_SQL + " WHERE id = ?", (resource_id,)
        ).fetchone()
        return _row_to_resource(row)

    def get(self, resource_id: str) -> Resource | None:
        row = self.conn.execute(
            _SELECT_SQL + " WHERE id = ?", (resource_id,)
        ).fetchone()
        return _row_to_resource(row) if row is not None else None

    def update_optimistic(self, change: ResourceUpdate, *, now_ms: int) -> Resource:
        """按 ``expected_version`` 做原子条件更新。

        - 资源物理不存在或已在回收站 => :class:`ResourceNotFoundError`
          （回收站资源在默认读写视角下等同于不存在）
        - 版本不匹配 => :class:`VersionConflictError`（携带期望/当前版本号）
        - name 唯一约束冲突 => :class:`UniqueConstraintError`（由连接层翻译）
        """
        current = self.get(change.id)
        if current is None or current.status != STATUS_ACTIVE:
            raise ResourceNotFoundError(f"资源不存在: id={change.id}")

        params = {
            "id": change.id,
            "expected_version": change.expected_version,
            "name": change.name if change.name is not None else current.name,
            "payload": change.payload if change.payload is not None else current.payload,
            "now": now_ms,
        }
        cur = self.conn.execute(_UPDATE_SQL, params)
        if cur.rowcount == 0:
            latest = self.get(change.id)
            current_version = latest.version if latest is not None else -1
            raise VersionConflictError(
                "版本冲突：资源已被其他请求修改，请基于最新版本重试",
                resource_id=change.id,
                expected_version=change.expected_version,
                current_version=current_version,
            )
        updated = self.get(change.id)
        assert updated is not None
        return updated

    def soft_delete(self, change, *, now_ms: int) -> Resource:
        """带乐观锁的软删除：仅正常资源可删。

        判定顺序：资源存在性 -> 版本号 -> 当前状态。

        - 物理不存在（从未创建 / 已被物理清理）=> :class:`ResourceNotFoundError`；
        - 版本号落后（这期间被别人更新/删除/恢复过）=>
          :class:`VersionConflictError`，哪怕并发结果恰好让资源进了回收站，
          调用方真正需要知道的也是“自己手里的版本已经落后”；
        - 版本就是最新、只是状态不适合（拿着当前版本对已删除资源再删）=>
          :class:`InvalidStateError`。

        条件 UPDATE 同时要求版本与状态匹配，失败的请求既不会被当成成功，
        也不会覆盖他人先提交的结果。
        """
        current = self.get(change.id)
        if current is None:
            raise ResourceNotFoundError(f"资源不存在: id={change.id}")
        if current.version != change.expected_version:
            raise VersionConflictError(
                "版本冲突：资源已被其他请求修改，请基于最新版本重试",
                resource_id=change.id,
                expected_version=change.expected_version,
                current_version=current.version,
            )
        if current.status != STATUS_ACTIVE:
            raise InvalidStateError(
                "资源已在回收站，无需重复删除",
                resource_id=change.id,
                current_status=current.status,
            )
        cur = self.conn.execute(
            _SOFT_DELETE_SQL,
            {"id": change.id, "expected_version": change.expected_version, "now": now_ms},
        )
        if cur.rowcount == 0:
            self._raise_post_check(change.id, change.expected_version)
        deleted = self.get(change.id)
        assert deleted is not None
        return deleted

    def restore(self, change, *, now_ms: int) -> Resource:
        """带乐观锁的恢复：仅回收站资源可恢复。

        判定顺序与 :meth:`soft_delete` 一致：存在性 -> 版本号 -> 当前状态。
        两个拿着同一版本的并发恢复竞争时，胜者恢复成功，败者在锁内重读会发现
        版本已推进（资源已被恢复），归 :class:`VersionConflictError`，绝不会把
        已恢复资源再改一次。名字已被其他正常资源占用时，partial unique index
        触发冲突，由连接层翻译为 :class:`UniqueConstraintError`。
        """
        current = self.get(change.id)
        if current is None:
            raise ResourceNotFoundError(f"资源不存在: id={change.id}")
        if current.version != change.expected_version:
            raise VersionConflictError(
                "版本冲突：资源已被其他请求修改，请基于最新版本重试",
                resource_id=change.id,
                expected_version=change.expected_version,
                current_version=current.version,
            )
        if current.status != STATUS_DELETED:
            raise InvalidStateError(
                "资源处于正常状态，无需恢复",
                resource_id=change.id,
                current_status=current.status,
            )
        cur = self.conn.execute(
            _RESTORE_SQL,
            {"id": change.id, "expected_version": change.expected_version, "now": now_ms},
        )
        if cur.rowcount == 0:
            self._raise_post_check(change.id, change.expected_version)
        restored = self.get(change.id)
        assert restored is not None
        return restored

    def _raise_post_check(self, resource_id: str, expected_version: int) -> None:
        """条件 UPDATE 影响 0 行后的归因（防御性二次判定）。

        前置校验在同一个 ``BEGIN IMMEDIATE`` 写事务内完成，正常串行情况下不会
        走到这里；此方法只在前置快照与提交瞬间不一致时兜底。归因仍严格按
        存在性 -> 版本号 -> 状态的顺序重新判定，保证三种原因始终分得清，
        且不会把并发失败误报成状态不允许。
        """
        latest = self.get(resource_id)
        if latest is None:
            raise ResourceNotFoundError(f"资源不存在: id={resource_id}")
        if latest.version != expected_version:
            raise VersionConflictError(
                "版本冲突：资源已被其他请求修改，请基于最新版本重试",
                resource_id=resource_id,
                expected_version=expected_version,
                current_version=latest.version,
            )
        raise InvalidStateError(
            "资源当前状态不允许该操作",
            resource_id=resource_id,
            current_status=latest.status,
        )

    def purge_expired(
        self, *, now_ms: int, retention_ms: int, batch_size: int
    ) -> tuple[int, int, bool]:
        """物理清除一批严格超过保留期的回收站资源。

        到期判定为 ``deleted_at + retention_ms < now``（严格小于，
        ``deleted_at == now - retention_ms`` 仍在保留期内，不删）。

        返回 ``(本批清除条数, 本批扫描候选数, 清理后是否仍有到期积压)``。
        ``has_more`` 反映**本批删除之后**的真实剩余情况：删除后在同一事务里
        再探测一次到期集合，最后一批恰好清空时返回 ``False``，调用方不会被
        误导继续跑。保留期内、已恢复或被重新删除（deleted_at 刷新）的行不受
        影响；重试只可能看到仍到期的行，不会重复清理已提交删除的行。
        """
        cutoff = now_ms - retention_ms
        rows = self.conn.execute(
            _PURGE_SELECT_SQL, {"cutoff": cutoff, "limit": batch_size}
        ).fetchall()
        ids = [r["id"] for r in rows]
        purged = 0
        if ids:
            placeholders = ",".join(f":id{i}" for i in range(len(ids)))
            params = {f"id{i}": value for i, value in enumerate(ids)}
            cur = self.conn.execute(_PURGE_DELETE_SQL.format(ids=placeholders), params)
            purged = cur.rowcount
        # 删除后再探测：本批没取满说明本来就没有更多；取满了也要看删完后是否
        # 真的还有到期行（最后一批恰好清空的关键场景）。
        has_more = len(ids) >= batch_size and self.conn.execute(
            _PURGE_REMAINING_SQL, {"cutoff": cutoff}
        ).fetchone() is not None
        return purged, len(ids), has_more

    def query_page(self, query: ResourceQuery) -> Page:
        """按固定排序键 ``(created_at ASC, id ASC)`` 做 keyset 分页。

        取 page_size+1 行判断是否还有下一页；游标非法时抛
        :class:`InvalidCursorError`。
        """
        page_size = min(max(query.page_size, 1), MAX_PAGE_SIZE)
        where: list[str] = []
        params: list[object] = []
        if query.status != "all":
            where.append("status = ?")
            params.append(query.status)
        if query.name_contains:
            where.append("name LIKE ?")
            params.append(f"%{query.name_contains}%")
        if query.cursor is not None:
            cursor_ts, cursor_id = decode_cursor(query.cursor)
            where.append("(created_at, id) > (?, ?)")
            params.extend([cursor_ts, cursor_id])

        sql = _SELECT_SQL
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(page_size + 1)

        rows = self.conn.execute(sql, params).fetchall()
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        items = [_row_to_resource(row) for row in page_rows]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
        )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

"""资源仓储：所有 SQL 读写集中于此。

行映射、乐观锁更新与游标分页（keyset pagination）均在本文件实现。
软删除（回收站）/ 恢复同样是携带版本号与目标状态的原子条件更新。
"""
from __future__ import annotations

import sqlite3

from app.cursor import decode_cursor, encode_cursor
from app.errors import (
    ResourceNotFoundError,
    ResourceStateError,
    VersionConflictError,
)
from app.models import (
    Page,
    Resource,
    ResourceCreate,
    ResourceQuery,
    ResourceStatus,
    ResourceUpdate,
)

MAX_PAGE_SIZE = 100

_INSERT_SQL = (
    "INSERT INTO resources (id, name, payload, version, created_at, updated_at) "
    "VALUES (:id, :name, :payload, 1, :now, :now)"
)

_UPDATE_SQL = (
    "UPDATE resources SET name = :name, payload = :payload, "
    "version = version + 1, updated_at = :now "
    "WHERE id = :id AND version = :expected_version AND status = 'active'"
)

_DELETE_SQL = (
    "UPDATE resources SET status = 'deleted', deleted_at = :now, "
    "version = version + 1, updated_at = :now "
    "WHERE id = :id AND version = :expected_version AND status = 'active'"
)

_RESTORE_SQL = (
    "UPDATE resources SET status = 'active', deleted_at = NULL, "
    "version = version + 1, updated_at = :now "
    "WHERE id = :id AND version = :expected_version AND status = 'deleted'"
)

_PURGE_SQL = (
    "DELETE FROM resources WHERE id IN ("
    "SELECT id FROM resources "
    "WHERE status = 'deleted' AND deleted_at < :cutoff "
    "ORDER BY deleted_at ASC, id ASC LIMIT :limit)"
)

_EXPIRED_EXISTS_SQL = (
    "SELECT EXISTS(SELECT 1 FROM resources "
    "WHERE status = 'deleted' AND deleted_at < :cutoff) AS remaining"
)

_SELECT_SQL = (
    "SELECT id, name, payload, version, created_at, updated_at, "
    "status, deleted_at FROM resources"
)


def _row_to_resource(row: sqlite3.Row) -> Resource:
    return Resource(
        id=row["id"],
        name=row["name"],
        payload=row["payload"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=ResourceStatus(row["status"]),
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
        try:
            self.conn.execute(_INSERT_SQL, params)
        except sqlite3.Error:
            raise
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

        失败分类（版本优先于状态，调用方据此决定“重读”还是“改操作”）：
        - 资源不存在 => :class:`ResourceNotFoundError`
        - 版本不匹配（含期间被他人删除）=> :class:`VersionConflictError`
        - 版本匹配但资源在回收站 => :class:`ResourceStateError`
        - name 唯一约束冲突 => :class:`UniqueConstraintError`（由连接层翻译）
        """
        current = self.get(change.id)
        if current is None:
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
            self._raise_conflict(
                change.id, change.expected_version,
                expect_state=ResourceStatus.ACTIVE,
                stale_msg="版本冲突：资源已被其他请求修改，请基于最新版本重试",
            )
        updated = self.get(change.id)
        assert updated is not None
        return updated

    def delete_soft(self, change, *, now_ms: int) -> Resource:
        """软删除：active + 版本匹配时原子地移入回收站，version +1。"""
        current = self.get(change.id)
        if current is None:
            raise ResourceNotFoundError(f"资源不存在: id={change.id}")
        params = {"id": change.id, "expected_version": change.expected_version, "now": now_ms}
        cur = self.conn.execute(_DELETE_SQL, params)
        if cur.rowcount == 0:
            self._raise_conflict(
                change.id, change.expected_version,
                expect_state=ResourceStatus.ACTIVE,
                stale_msg="版本冲突：资源已被其他请求修改或删除，请基于最新版本重试",
            )
        deleted = self.get(change.id)
        assert deleted is not None
        return deleted

    def restore(self, change, *, now_ms: int) -> Resource:
        """恢复：deleted + 版本匹配时原子地还原为 active，version 继续 +1。"""
        current = self.get(change.id)
        if current is None:
            raise ResourceNotFoundError(f"资源不存在: id={change.id}")
        params = {"id": change.id, "expected_version": change.expected_version, "now": now_ms}
        cur = self.conn.execute(_RESTORE_SQL, params)
        if cur.rowcount == 0:
            self._raise_conflict(
                change.id, change.expected_version,
                expect_state=ResourceStatus.DELETED,
                stale_msg="版本冲突：回收站中的资源版本已变化，请基于最新版本重试",
            )
        restored = self.get(change.id)
        assert restored is not None
        return restored

    def _raise_conflict(
        self,
        resource_id: str,
        expected_version: int,
        *,
        expect_state: ResourceStatus,
        stale_msg: str,
    ) -> None:
        """条件更新影响 0 行时，按“先版本、后状态”区分拒绝原因。

        版本不一致 => 期间被他人改动（改/删/恢复都让版本前进），属版本冲突，
        调用方应重读最新版本；版本一致却仍不匹配，才是状态不允许。
        """
        latest = self.get(resource_id)
        if latest is None:
            # 同一事务内刚读过仍消失（理论上不会发生），归为不存在
            raise ResourceNotFoundError(f"资源不存在: id={resource_id}")
        if latest.version != expected_version:
            raise VersionConflictError(
                stale_msg,
                resource_id=resource_id,
                expected_version=expected_version,
                current_version=latest.version,
            )
        raise ResourceStateError(
            f"资源当前状态为 {latest.status.value}，不允许该操作",
            resource_id=resource_id,
            current_state=latest.status.value,
        )

    def query_page(self, query: ResourceQuery) -> Page:
        """按固定排序键 ``(created_at ASC, id ASC)`` 做 keyset 分页。

        可按状态筛选（active / deleted）；``all`` 不加状态条件。
        取 page_size+1 行判断是否还有下一页；游标非法时抛
        :class:`InvalidCursorError`。游标只表示排序位置，与状态筛选无关，
        因此同一游标配合不同筛选仍是“该筛选集合中位置之后”的稳定结果。
        """
        page_size = min(max(query.page_size, 1), MAX_PAGE_SIZE)
        where: list[str] = []
        params: list[object] = []
        if query.status is not ResourceStatus.ALL:
            where.append("status = ?")
            params.append(query.status.value)
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

    def purge_expired(self, *, cutoff_ms: int, batch_size: int) -> tuple[int, bool]:
        """物理删除一批 ``deleted_at < cutoff_ms`` 的回收站记录。

        固定按 ``(deleted_at ASC, id ASC)`` 删除，保证分批顺序确定、可重入：
        已删的行自然不满足条件，重试不会重复清理，也不会触碰保留期内的数据。
        返回 ``(本批删除行数, 是否还有超期记录)``。
        """
        cur = self.conn.execute(
            _PURGE_SQL, {"cutoff": cutoff_ms, "limit": batch_size}
        )
        deleted_count = cur.rowcount
        remaining = self.conn.execute(
            _EXPIRED_EXISTS_SQL, {"cutoff": cutoff_ms}
        ).fetchone()["remaining"]
        return deleted_count, bool(remaining)

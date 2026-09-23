"""资源仓储：所有 SQL 读写集中于此。

行映射、乐观锁更新与游标分页（keyset pagination）均在本文件实现。
"""
from __future__ import annotations

import sqlite3

from app.cursor import decode_cursor, encode_cursor
from app.errors import ResourceNotFoundError, VersionConflictError
from app.models import Page, Resource, ResourceCreate, ResourceQuery, ResourceUpdate

MAX_PAGE_SIZE = 100

_INSERT_SQL = (
    "INSERT INTO resources (id, name, payload, version, created_at, updated_at) "
    "VALUES (:id, :name, :payload, 1, :now, :now)"
)

_UPDATE_SQL = (
    "UPDATE resources SET name = :name, payload = :payload, "
    "version = version + 1, updated_at = :now "
    "WHERE id = :id AND version = :expected_version"
)

_SELECT_SQL = (
    "SELECT id, name, payload, version, created_at, updated_at FROM resources"
)


def _row_to_resource(row: sqlite3.Row) -> Resource:
    return Resource(
        id=row["id"],
        name=row["name"],
        payload=row["payload"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
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

        - 资源不存在 => :class:`ResourceNotFoundError`
        - 版本不匹配 => :class:`VersionConflictError`（携带期望/当前版本号）
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

    def query_page(self, query: ResourceQuery) -> Page:
        """按固定排序键 ``(created_at ASC, id ASC)`` 做 keyset 分页。

        取 page_size+1 行判断是否还有下一页；游标非法时抛
        :class:`InvalidCursorError`。
        """
        page_size = min(max(query.page_size, 1), MAX_PAGE_SIZE)
        where: list[str] = []
        params: list[object] = []
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

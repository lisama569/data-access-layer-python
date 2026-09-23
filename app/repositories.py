"""资源仓储：基于 SQLite 的持久化数据访问。

排序键固定为 (created_at ASC, id ASC)，保证全序、稳定、可解释；
乐观锁通过条件更新 ``WHERE id=? AND version=?`` 实现：
并发的两个更新在 SQLite 写串行化下，后到者会在前者提交后以新版本求值，
因版本不再匹配而影响 0 行，从而被准确归类为 VersionConflictError。
所有 sqlite3 异常在此转译为 app.errors 中的对外错误。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app.errors import NotFoundError, StorageError, VersionConflictError
from app.models import Page, Resource
from app.pagination import clamp_limit, decode_cursor, encode_cursor

_SELECT_COLS = "id, name, status, version, created_at, updated_at"


def utc_now_iso() -> str:
    """UTC ISO-8601（微秒精度、固定 +00:00），保证字符串字典序==时间序。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _row_to_resource(row: sqlite3.Row) -> Resource:
    return Resource(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ResourceRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, name: str, status: str) -> Resource:
        """新增资源，version 初始为 1。"""
        resource_id = uuid.uuid4().hex
        now = utc_now_iso()
        try:
            self.conn.execute(
                "INSERT INTO resources (id, name, status, version, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?)",
                (resource_id, name, status, now, now),
            )
            row = self.conn.execute(
                f"SELECT {_SELECT_COLS} FROM resources WHERE id = ?",
                (resource_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("failed to create resource") from exc
        return _row_to_resource(row)

    def get(self, resource_id: str) -> Resource:
        """按主键读取；不存在抛 NotFoundError。"""
        try:
            row = self.conn.execute(
                f"SELECT {_SELECT_COLS} FROM resources WHERE id = ?",
                (resource_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("failed to read resource") from exc
        if row is None:
            raise NotFoundError(f"resource not found: {resource_id}")
        return _row_to_resource(row)

    def update(
        self,
        resource_id: str,
        name: str,
        status: str,
        expected_version: int,
    ) -> Resource:
        """基于版本号的乐观更新。

        版本不匹配抛 VersionConflictError（携带期望/实际版本号）；
        资源不存在抛 NotFoundError。
        """
        now = utc_now_iso()
        try:
            cursor = self.conn.execute(
                "UPDATE resources"
                " SET name = ?, status = ?, version = version + 1, updated_at = ?"
                " WHERE id = ? AND version = ?",
                (name, status, now, resource_id, expected_version),
            )
            if cursor.rowcount == 1:
                row = self.conn.execute(
                    f"SELECT {_SELECT_COLS} FROM resources WHERE id = ?",
                    (resource_id,),
                ).fetchone()
                return _row_to_resource(row)

            # 影响 0 行：区分"不存在"与"版本冲突"。
            current = self.conn.execute(
                "SELECT version FROM resources WHERE id = ?",
                (resource_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("failed to update resource") from exc

        if current is None:
            raise NotFoundError(f"resource not found: {resource_id}")
        raise VersionConflictError(
            "resource was modified by another transaction",
            resource_id=resource_id,
            expected_version=expected_version,
            actual_version=current["version"],
        )

    def list_page(
        self,
        *,
        limit: int = 20,
        cursor: str = "",
        status: str | None = None,
        name_contains: str | None = None,
    ) -> Page[Resource]:
        """键集游标分页查询；非法游标抛 InvalidCursorError。

        多取 1 行（limit+1）判定是否有下一页；next_cursor 指向本页最后一条。
        """
        page_size = clamp_limit(limit)
        clauses: list[str] = []
        params: list[str] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if name_contains is not None:
            # 转义 LIKE 通配符，使过滤语义可解释、不被用户输入破坏。
            needle = name_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{needle}%")
        if cursor:
            bookmark = decode_cursor(cursor)  # 非法即抛 InvalidCursorError
            # 键集谓词：(created_at, id) 严格大于游标位置。
            clauses.append(
                "(created_at > ? OR (created_at = ? AND id > ?))"
            )
            params.extend([bookmark.created_at, bookmark.created_at, bookmark.id])

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT {_SELECT_COLS} FROM resources{where_sql}"
            " ORDER BY created_at ASC, id ASC"
            " LIMIT ?"
        )
        params.append(str(page_size + 1))

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("failed to list resources") from exc

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        items = [_row_to_resource(r) for r in page_rows]
        next_cursor = (
            encode_cursor(page_rows[-1]["created_at"], page_rows[-1]["id"])
            if has_more and page_rows
            else ""
        )
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

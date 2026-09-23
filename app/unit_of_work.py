"""请求级事务边界（Unit of Work）。

一个 UnitOfWork 对应一个数据库连接与一个显式事务：
- __enter__ 执行 ``BEGIN IMMEDIATE``（立即拿写锁，写入串行化、杜绝脏读）；
- commit() 提交多步写入；rollback() 整体回滚；
- with 块内出现任何异常都会自动回滚后再向外抛，绝不残留部分数据；
- 退出时关闭连接。

底层 sqlite3 异常一律转译为 StorageError，业务错误（app.errors）原样传播。
"""

from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Self

from app.database import create_connection
from app.errors import StorageError
from app.repositories import ResourceRepository


class UnitOfWork:
    def __init__(self, db_path: str | None = None) -> None:
        self._conn = create_connection(db_path)
        self._resources = ResourceRepository(self._conn)
        self._active = False
        self._closed = False

    def __enter__(self) -> Self:
        try:
            # IMMEDIATE：进入即获取保留锁，把写冲突前置到事务开头。
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self._conn.close()
            self._closed = True
            raise StorageError("failed to begin transaction") from exc
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc is not None and self._active:
                # 块内抛出任何异常：整体回滚，保证不残留部分写入。
                self._rollback_silent()
        finally:
            if not self._closed:
                self._conn.close()
                self._closed = True
        # 返回 None：不吞异常，交由上层归类处理。

    @property
    def resources(self) -> ResourceRepository:
        """返回事务绑定的资源仓储。"""
        return self._resources

    def commit(self) -> None:
        """提交事务；事务已结束或提交失败抛 StorageError。"""
        if not self._active:
            raise StorageError("transaction is not active")
        try:
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback_silent()
            raise StorageError("failed to commit transaction") from exc
        self._active = False

    def rollback(self) -> None:
        """回滚事务。"""
        if not self._active:
            raise StorageError("transaction is not active")
        self._rollback_silent()

    def _rollback_silent(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            # 回滚失败（如连接已断）只能关闭连接，避免状态外泄。
            pass
        finally:
            self._active = False

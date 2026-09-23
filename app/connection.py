"""本地 SQLite 连接管理。

- 文件库使用 WAL 模式，允许读写并发；写操作通过 busy_timeout 等待锁。
- 连接使用 autocommit 模式（isolation_level=None），事务边界由
  ``app.transaction.transactional`` 显式控制（BEGIN IMMEDIATE / COMMIT / ROLLBACK）。
- 所有 ``sqlite3.Error`` 在此层或仓储层被翻译为 ``app.errors`` 中的对外错误，
  底层异常类型不直接透出。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.errors import StorageError, UniqueConstraintError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS resources (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

BUSY_TIMEOUT_MS = 5_000


def translate_sqlite_error(exc: sqlite3.Error) -> Exception:
    """把底层 SQLite 异常翻译为对外错误类型。"""
    if isinstance(exc, sqlite3.IntegrityError):
        msg = str(exc).lower()
        if "unique" in msg:
            return UniqueConstraintError(
                f"唯一约束冲突: {exc}", cause=exc
            )
        return StorageError(f"数据完整性错误: {exc}", cause=exc)
    return StorageError(f"本地存储错误: {exc}", cause=exc)


class Database:
    """SQLite 数据库封装：建表、连接工厂。"""

    def __init__(self, path: str) -> None:
        self.path = path

    @property
    def is_memory(self) -> bool:
        return self.path == ":memory:"

    def initialize(self) -> None:
        """创建表结构；幂等，可重复调用。"""
        try:
            conn = self.connect()
            try:
                if not self.is_memory:
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if not self.is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


@contextmanager
def get_connection(db: Database) -> Iterator[sqlite3.Connection]:
    """提供连接并在退出时关闭；非事务场景使用。"""
    conn = db.connect()
    try:
        yield conn
    except sqlite3.Error as exc:
        raise translate_sqlite_error(exc) from exc
    finally:
        conn.close()

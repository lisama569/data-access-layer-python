"""本地 SQLite 连接与建表管理。

设计要点：
- WAL 日志 + busy_timeout：读不阻塞写、写串行化，降低 SQLITE_BUSY；
- 显式外键约束；行工厂为 sqlite3.Row；
- resources 表对排序键 (created_at, id) 建索引以支持键集分页。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_DB_PATH = "data.db"
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_resources_created_id
    ON resources (created_at, id);
CREATE INDEX IF NOT EXISTS ix_resources_status_created_id
    ON resources (status, created_at, id);
"""


def get_db_path() -> str:
    """返回当前生效的数据库文件路径（可由环境变量 DAL_DB_PATH 覆盖）。"""
    return os.environ.get("DAL_DB_PATH") or DEFAULT_DB_PATH


def create_connection(db_path: str | None = None) -> sqlite3.Connection:
    """创建一个开启外键、WAL 与忙等待的连接。"""
    path = db_path or get_db_path()
    conn = sqlite3.connect(
        path,
        timeout=_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level="",  # 关闭隐式事务，事务边界由 UnitOfWork 显式管理
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """创建 resources 表与索引（幂等）。"""
    conn.executescript(_SCHEMA)


@contextmanager
def connect(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """简单连接上下文：正常退出提交、异常回滚并关闭。

    需要显式事务边界的场景请使用 UnitOfWork。
    """
    conn = create_connection(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

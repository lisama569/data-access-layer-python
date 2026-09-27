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
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    deleted_at INTEGER
);
"""

# 仅正常状态的资源占用名字：删除即释放名字；恢复时若名字已被其他正常
# 资源占用，由该索引触发唯一约束冲突，保证绝不出现两个同名正常资源。
ACTIVE_NAME_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "ux_resources_active_name ON resources(name) WHERE status = 'active'"
)

BUSY_TIMEOUT_MS = 5_000


def _needs_migration(conn: sqlite3.Connection) -> bool:
    """旧结构没有 status 列；资源表不存在时无需迁移（SCHEMA_SQL 会新建）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resources'"
    ).fetchone()
    if row is None:
        return False
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(resources)")}
    return "status" not in columns


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """把旧表整表搬运到带生命周期列的新结构。

    SQLite 无法直接 DROP COLUMN（旧版本）/删除列上的表级约束，故改名后
    重建：旧数据 status 置为 active、deleted_at 置空，版本号原样保留。
    """
    conn.execute("ALTER TABLE resources RENAME TO resources_legacy")
    conn.execute(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO resources "
        "(id, name, payload, version, created_at, updated_at, status, deleted_at) "
        "SELECT id, name, payload, version, created_at, updated_at, 'active', NULL "
        "FROM resources_legacy"
    )
    conn.execute("DROP TABLE resources_legacy")


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
        """创建表结构；幂等，可重复调用。

        对早期建表（无 ``status`` / ``deleted_at`` 列、``name`` 上为表级
        UNIQUE 约束）的库做一次性在线迁移：整表搬运到新结构，旧数据全部
        视为正常资源，迁移包在一个事务里，失败不留下半张表。
        """
        try:
            conn = self.connect()
            try:
                if not self.is_memory:
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(SCHEMA_SQL)
                if _needs_migration(conn):
                    _migrate_schema(conn)
                conn.execute(ACTIVE_NAME_INDEX_SQL)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
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

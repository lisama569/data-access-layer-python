"""请求级事务边界管理。

用法::

    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        repo.insert(...)
        repo.update_optimistic(...)
    # 离开 with 块：无异常 => COMMIT；任何异常 => ROLLBACK 后原样/归类抛出

规则：
- 使用 ``BEGIN IMMEDIATE`` 在事务开始时即取得保留锁，写事务之间串行，
  锁竞争由 busy_timeout 等待，等待超时仍失败时归类为 StorageError。
- 已是 :class:`app.errors.DataAccessError` 的异常保持类型不变重新抛出
  （冲突原因必须可区分），其余 ``sqlite3.Error`` 被翻译，
  其他意外异常包装为 :class:`TransactionError`；底层异常不直接透出。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.connection import Database, translate_sqlite_error
from app.errors import DataAccessError, TransactionError


@contextmanager
def transactional(db: Database) -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    finished = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        try:
            conn.execute("COMMIT")
            finished = True
        except sqlite3.Error as exc:
            # 提交本身失败：尽力回滚，再透出归类后的错误
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise translate_sqlite_error(exc) from exc
    except DataAccessError:
        # 已归类错误（版本冲突、唯一约束、非法游标等）：回滚后原样抛出
        if not finished:
            _safe_rollback(conn)
        raise
    except sqlite3.Error as exc:
        _safe_rollback(conn)
        raise translate_sqlite_error(exc) from exc
    except Exception as exc:
        _safe_rollback(conn)
        raise TransactionError(f"事务失败，已整体回滚: {exc}", cause=exc) from exc
    finally:
        conn.close()


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        # 回滚失败时不再掩盖原始异常；连接关闭后事务自然终结
        pass

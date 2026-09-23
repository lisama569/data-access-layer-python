"""事务边界测试。

覆盖：
- 同事务多步写入成功时全部提交；
- 任一步失败（含唯一约束冲突）时整体回滚，无部分数据残留；
- 显式回滚的事务不留数据；
- 底层 sqlite3 异常被翻译为 DataAccessError，原始异常类型不外泄。
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from app.errors import (
    DataAccessError,
    StorageError,
    TransactionError,
    UniqueConstraintError,
)
from app.models import ResourceCreate, ResourceQuery
from app.repository import ResourceRepository
from app.service import ResourceService
from app.transaction import transactional

logger = logging.getLogger("app.dal")


def test_batch_commit(service: ResourceService) -> None:
    count = service.batch_create(
        [ResourceCreate(name=f"item-{i}") for i in range(3)]
    )
    logger.info("[TEST] batch committed count=%d", count)
    assert count == 3
    page = service.list_resources(ResourceQuery(page_size=50))
    assert len(page.items) == 3


def test_batch_rollback_on_failure(service: ResourceService) -> None:
    service.create_resource(ResourceCreate(name="dup-name"))
    with pytest.raises(UniqueConstraintError) as exc_info:
        # 前两条正常，第三条撞上唯一约束：整批必须回滚
        service.batch_create(
            [
                ResourceCreate(name="new-1"),
                ResourceCreate(name="new-2"),
                ResourceCreate(name="dup-name"),
            ]
        )
    logger.info("[TEST] batch rollback code=%s", exc_info.value.code)
    assert exc_info.value.code == "unique_constraint_violation"

    from app.models import ResourceQuery

    remaining = service.list_resources(ResourceQuery(page_size=50)).items
    names = sorted(r.name for r in remaining)
    logger.info("[TEST] remaining after rollback=%s", names)
    assert names == ["dup-name"]  # new-1/new-2 均不得残留


def test_explicit_exception_rolls_back(service: ResourceService, db) -> None:
    with pytest.raises(TransactionError) as exc_info:
        with transactional(db) as conn:
            ResourceRepository(conn).insert(
                ResourceCreate(name="will-rollback"), now_ms=1, resource_id="r1"
            )
            raise RuntimeError("simulated business failure")
    logger.info("[TEST] explicit rollback code=%s", exc_info.value.code)
    assert exc_info.value.code == "transaction_failed"
    from app.models import ResourceQuery

    assert service.list_resources(ResourceQuery(page_size=10)).items == []


def test_storage_error_wrapped(service: ResourceService, db) -> None:
    """底层 sqlite3.Error 不得原样透出：坏 SQL 被归类为 StorageError。"""
    with pytest.raises(DataAccessError) as exc_info:
        with transactional(db) as conn:
            conn.execute("INSERT INTO no_such_table VALUES (1)")
    assert isinstance(exc_info.value, StorageError)
    logger.info(
        "[TEST] storage error wrapped code=%s type=%s",
        exc_info.value.code, type(exc_info.value).__name__,
    )
    assert not isinstance(exc_info.value, sqlite3.Error)

    # 该事务回滚后，库仍可正常使用（没有残留坏事务状态）
    resource = service.create_resource(ResourceCreate(name="after-failure"))
    assert resource.version == 1

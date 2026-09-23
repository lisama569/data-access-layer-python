"""并发更新 / 乐观锁行为测试。

覆盖：
- 同版本更新成功且版本号 +1；
- 过期版本被拒绝，VersionConflictError 携带可区分的期望/当前版本；
- 两个并发事务竞争同一行，恰好一个成功、一个拿到 version_conflict，
  失败方绝不覆盖已提交变更；
- 更新不存在的资源归类为 resource_not_found。
"""
from __future__ import annotations

import logging
import threading

import pytest

from app.connection import Database
from app.errors import ResourceNotFoundError, VersionConflictError
from app.models import ResourceCreate, ResourceUpdate
from app.service import ResourceService
from app.transaction import transactional
from app.repository import ResourceRepository

logger = logging.getLogger("app.dal")


def test_optimistic_update_succeeds(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc", payload="v0"))
    updated = service.update_resource(
        ResourceUpdate(id=created.id, expected_version=1, payload="v1")
    )
    logger.info("[TEST] update success id=%s version=%d", updated.id, updated.version)
    assert updated.version == 2
    assert updated.payload == "v1"


def test_stale_version_rejected(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    service.update_resource(
        ResourceUpdate(id=created.id, expected_version=1, payload="v1")
    )
    with pytest.raises(VersionConflictError) as exc_info:
        service.update_resource(
            ResourceUpdate(id=created.id, expected_version=1, payload="stale")
        )
    err = exc_info.value
    logger.info(
        "[TEST] stale rejected id=%s expected_version=%d current_version=%d",
        err.resource_id, err.expected_version, err.current_version,
    )
    assert err.code == "version_conflict"
    assert err.expected_version == 1
    assert err.current_version == 2
    # 已提交内容没有被覆盖
    assert service.get_resource(created.id).payload == "v1"


def test_concurrent_updates_only_one_wins(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="counter", payload="0"))
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(thread_name: str, payload: str) -> None:
        barrier.wait()
        try:
            resource = service.update_resource(
                ResourceUpdate(
                    id=created.id, expected_version=1, payload=payload
                )
            )
            results[thread_name] = ("committed", resource.version)
            logger.info(
                "[TEST] %s committed version=%d", thread_name, resource.version
            )
        except VersionConflictError as exc:
            results[thread_name] = (exc.code, exc.current_version)
            logger.info(
                "[TEST] %s rejected expected=%d current=%d",
                thread_name, exc.expected_version, exc.current_version,
            )

    t1 = threading.Thread(target=worker, args=("worker-1", "A"))
    t2 = threading.Thread(target=worker, args=("worker-2", "B"))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = {k: v[0] for k, v in results.items()}
    logger.info("[TEST] concurrent results=%s", results)
    assert sorted(statuses.values()) == ["committed", "version_conflict"]

    final = service.get_resource(created.id)
    winner = next(k for k, v in results.items() if v[0] == "committed")
    expected_payload = {"worker-1": "A", "worker-2": "B"}[winner]
    assert final.payload == expected_payload
    assert final.version == 2
    # 再用当前版本更新一次应成功，证明冲突后状态可继续演进
    again = service.update_resource(
        ResourceUpdate(id=created.id, expected_version=2, payload="C")
    )
    assert again.version == 3


def test_missing_resource_update_not_found(service: ResourceService) -> None:
    with pytest.raises(ResourceNotFoundError) as exc_info:
        service.update_resource(
            ResourceUpdate(id="missing", expected_version=1, payload="x")
        )
    logger.info("[TEST] missing update code=%s", exc_info.value.code)
    assert exc_info.value.code == "resource_not_found"

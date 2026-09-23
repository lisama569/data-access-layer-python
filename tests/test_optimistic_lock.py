"""并发更新 / 乐观锁冲突归类测试。"""

from __future__ import annotations

import logging
import threading

import pytest

from app.errors import VersionConflictError
from app.unit_of_work import UnitOfWork

logger = logging.getLogger("dal.tests")


def test_concurrent_update_conflict(service) -> None:
    """两个线程都基于 version=1 更新同一资源：恰好一个提交，另一个被拒绝。"""
    resource = service.create_resource("shared", "active")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    versions: list[int] = []
    lock = threading.Lock()

    def worker(new_name: str) -> None:
        barrier.wait()  # 尽量让两个事务同时开始
        try:
            with UnitOfWork(service.db_path) as uow:
                updated = uow.resources.update(resource.id, new_name, "active", 1)
                uow.commit()
            with lock:
                outcomes.append("committed")
                versions.append(updated.version)
                logger.info(
                    "CONCURRENT UPDATE result=COMMITTED name=%s version=%s",
                    new_name,
                    updated.version,
                )
        except VersionConflictError as exc:
            with lock:
                outcomes.append("conflict")
                versions.append(exc.actual_version)
                logger.info(
                    "CONCURRENT UPDATE result=REJECTED reason=%s expected=%s actual=%s",
                    exc.reason,
                    exc.expected_version,
                    exc.actual_version,
                )

    t1 = threading.Thread(target=worker, args=("from-t1",))
    t2 = threading.Thread(target=worker, args=("from-t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(outcomes) == ["committed", "conflict"], outcomes
    final = service.get_resource(resource.id)
    assert final.version == 2  # 只有一次更新落地，他人变更未被覆盖
    logger.info("FINAL version=%s name=%s", final.version, final.name)


def test_repeated_update_with_stale_version_rejected(service) -> None:
    resource = service.create_resource("seq", "active")
    service.update_resource(resource.id, "seq-2", "active", 1)

    with pytest.raises(VersionConflictError) as exc_info:
        service.update_resource(resource.id, "seq-lost", "active", 1)
    err = exc_info.value
    assert err.reason == "version_conflict"
    assert err.expected_version == 1
    assert err.actual_version == 2
    assert err.resource_id == resource.id
    logger.info(
        "STALE UPDATE result=REJECTED reason=%s expected=%s actual=%s",
        err.reason,
        err.expected_version,
        err.actual_version,
    )

    # 用正确版本号仍可继续更新，冲突不会污染后续写入。
    recovered = service.update_resource(resource.id, "seq-3", "archived", 2)
    assert recovered.version == 3
    logger.info("RECOVERED UPDATE result=COMMITTED version=%s", recovered.version)

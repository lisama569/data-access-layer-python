"""事务提交与整体回滚测试。"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from app.errors import DALError, ConflictError, StorageError
from app.unit_of_work import UnitOfWork

logger = logging.getLogger("dal.tests")


def test_commit_persists_all_writes(service) -> None:
    with UnitOfWork(service.db_path) as uow:
        r1 = uow.resources.create("tx-a", "s")
        r2 = uow.resources.create("tx-b", "s")
        uow.commit()
    logger.info(
        "TRANSACTION result=COMMITTED ids=%s versions=%s",
        [r1.id, r2.id],
        [r1.version, r2.version],
    )
    assert service.get_resource(r1.id).version == 1
    assert service.get_resource(r2.id).version == 1


def test_rollback_leaves_no_partial_data(service) -> None:
    baseline = service.create_resource("baseline", "s")

    with pytest.raises(ConflictError) as exc_info:
        service.create_many_then_fail([("partial-1", "s"), ("partial-2", "s")])
    assert exc_info.value.reason == "multi_step_aborted"
    logger.info("TRANSACTION result=ROLLED_BACK reason=%s", exc_info.value.reason)

    page = service.list_resources(limit=50)
    names = {r.name for r in page.items}
    assert names == {"baseline"}
    assert service.get_resource(baseline.id).version == 1
    logger.info("POST-ROLLBACK rows=%s baseline_version=%s", names, 1)


def test_explicit_rollback(service) -> None:
    with UnitOfWork(service.db_path) as uow:
        uow.resources.create("explicit", "s")
        uow.rollback()
    logger.info("TRANSACTION result=ROLLED_BACK explicit=True")
    assert service.list_resources(limit=10).items == []


def test_storage_error_is_translated(service, monkeypatch) -> None:
    """仓储执行期底层 sqlite3 异常不得原样透出，必须归类为 StorageError。"""

    def boom(self, name, status):  # noqa: ANN001
        raise sqlite3.OperationalError("simulated disk I/O error")

    monkeypatch.setattr("app.repositories.ResourceRepository.create", boom)

    with pytest.raises(DALError) as exc_info:
        service.create_resource("will-fail", "s")
    assert isinstance(exc_info.value, StorageError)
    assert "simulated disk I/O error" not in str(exc_info.value)
    logger.info("TRANSACTION result=ROLLED_BACK storage_error_translated=True")


def test_broken_connection_during_commit_is_translated(service, monkeypatch) -> None:
    """提交阶段连接故障：对外仍为 StorageError，且事务整体回滚。"""
    real_commit = UnitOfWork.commit

    def failing_commit(self):  # noqa: ANN001
        self._conn.close()  # 让随后的 COMMIT 必失败
        return real_commit(self)

    monkeypatch.setattr(UnitOfWork, "commit", failing_commit)

    with pytest.raises(StorageError):
        service.create_resource("lost", "s")
    logger.info("COMMIT FAILURE result=ROLLED_BACK reason=storage_error")

    monkeypatch.undo()
    assert service.list_resources(limit=10).items == []

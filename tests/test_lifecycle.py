"""资源生命周期（软删除 / 恢复 / 回收站）行为测试。

覆盖：
- 软删除后默认单查、默认列表都视为不存在；回收站视图与 include_deleted 可见；
- 恢复后重新可读，版本号单调向前（删除、恢复都 +1），不回退不重复；
- 删除 / 恢复的并发保护，三类原因可区分：
  不存在 / 版本落后（期间被他人改、删） / 当前状态不允许；
- 名字在软删期间保留，只有物理清除后才允许新资源复用，恢复绝不撞名；
- 并发删除竞争：同版本并发，恰好一成一 version_conflict，绝不覆盖。
"""
from __future__ import annotations

import logging
import threading

import pytest

from app.errors import (
    ResourceNotFoundError,
    ResourceStateError,
    UniqueConstraintError,
    VersionConflictError,
)
from app.models import (
    ResourceCreate,
    ResourceDelete,
    ResourceQuery,
    ResourceRestore,
    ResourceStatus,
    ResourceUpdate,
)
from app.service import ResourceService

logger = logging.getLogger("app.dal")


def _create(service: ResourceService, name: str = "doc"):
    return service.create_resource(ResourceCreate(name=name))


# ---------- 基本删除 / 恢复 ----------

def test_soft_delete_hides_from_default_reads(service: ResourceService) -> None:
    r = _create(service)
    deleted = service.delete_resource(ResourceDelete(id=r.id, expected_version=1))

    assert deleted.version == 2
    assert deleted.status is ResourceStatus.DELETED
    assert deleted.deleted_at is not None

    # 默认单查视为不存在
    with pytest.raises(ResourceNotFoundError) as exc:
        service.get_resource(r.id)
    assert exc.value.code == "resource_not_found"

    # 默认列表不返回
    assert service.list_resources(ResourceQuery(page_size=10)).items == []

    # 回收站视图可见
    trash = service.list_resources(
        ResourceQuery(page_size=10, status=ResourceStatus.DELETED)
    )
    assert [i.id for i in trash.items] == [r.id]

    # 显式包含回收站的单查可见
    visible = service.get_resource(r.id, include_deleted=True)
    assert visible.version == 2 and visible.status is ResourceStatus.DELETED


def test_restore_brings_back_with_advancing_version(service: ResourceService) -> None:
    r = _create(service)
    service.delete_resource(ResourceDelete(id=r.id, expected_version=1))

    restored = service.restore_resource(ResourceRestore(id=r.id, expected_version=2))
    assert restored.version == 3
    assert restored.status is ResourceStatus.ACTIVE
    assert restored.deleted_at is None

    again = service.get_resource(r.id)
    assert again.version == 3
    # 恢复后默认列表重新可见
    ids = [i.id for i in service.list_resources(ResourceQuery(page_size=10)).items]
    assert ids == [r.id]


def test_version_advances_through_multiple_cycles(service: ResourceService) -> None:
    r = _create(service)
    v = 1
    for _ in range(3):  # create(v1) -> del -> restore ...
        d = service.delete_resource(ResourceDelete(id=r.id, expected_version=v))
        v += 1
        assert d.version == v
        rs = service.restore_resource(ResourceRestore(id=r.id, expected_version=v))
        v += 1
        assert rs.version == v
    # 版本始终单调向前，没有回退或重复
    assert v == 7


# ---------- 三类拒绝原因可区分 ----------

def test_delete_restore_missing_is_not_found(service: ResourceService) -> None:
    with pytest.raises(ResourceNotFoundError):
        service.delete_resource(ResourceDelete(id="ghost", expected_version=1))
    with pytest.raises(ResourceNotFoundError):
        service.restore_resource(ResourceRestore(id="ghost", expected_version=1))


def test_stale_delete_after_others_change_is_version_conflict(
    service: ResourceService,
) -> None:
    r = _create(service)
    # 别人先把它更新到 v2
    service.update_resource(
        ResourceUpdate(id=r.id, expected_version=1, payload="new")
    )
    with pytest.raises(VersionConflictError) as exc:
        service.delete_resource(ResourceDelete(id=r.id, expected_version=1))
    err = exc.value
    assert err.code == "version_conflict"
    assert err.expected_version == 1 and err.current_version == 2
    # 未被删除，他人提交的内容完好
    assert service.get_resource(r.id).payload == "new"


def test_stale_delete_after_others_delete_is_version_conflict(
    service: ResourceService,
) -> None:
    r = _create(service)
    service.delete_resource(ResourceDelete(id=r.id, expected_version=1))  # 别人删到 v2
    with pytest.raises(VersionConflictError) as exc:
        service.delete_resource(ResourceDelete(id=r.id, expected_version=1))
    assert exc.value.code == "version_conflict"
    assert exc.value.current_version == 2


def test_delete_already_deleted_at_current_version_is_state_conflict(
    service: ResourceService,
) -> None:
    r = _create(service)
    service.delete_resource(ResourceDelete(id=r.id, expected_version=1))
    with pytest.raises(ResourceStateError) as exc:
        service.delete_resource(ResourceDelete(id=r.id, expected_version=2))
    assert exc.value.code == "resource_state_conflict"
    assert exc.value.current_state == "deleted"


def test_update_deleted_resource_classification(service: ResourceService) -> None:
    r = _create(service)
    service.delete_resource(ResourceDelete(id=r.id, expected_version=1))
    # 旧版本更新回收站资源 => 版本冲突
    with pytest.raises(VersionConflictError):
        service.update_resource(
            ResourceUpdate(id=r.id, expected_version=1, payload="x")
        )
    # 当前版本更新回收站资源 => 状态冲突
    with pytest.raises(ResourceStateError) as exc:
        service.update_resource(
            ResourceUpdate(id=r.id, expected_version=2, payload="x")
        )
    assert exc.value.current_state == "deleted"


def test_restore_active_resource_classification(service: ResourceService) -> None:
    r = _create(service)  # active v1
    # 恢复一个本来就正常的资源：版本一致 => 状态冲突
    with pytest.raises(ResourceStateError):
        service.restore_resource(ResourceRestore(id=r.id, expected_version=1))


def test_stale_restore_after_others_restore_is_version_conflict(
    service: ResourceService,
) -> None:
    r = _create(service)
    service.delete_resource(ResourceDelete(id=r.id, expected_version=1))  # v2 deleted
    service.restore_resource(ResourceRestore(id=r.id, expected_version=2))  # v3 active
    # 另一个客户端拿着删除时的 v2 想恢复：版本已落后
    with pytest.raises(VersionConflictError) as exc:
        service.restore_resource(ResourceRestore(id=r.id, expected_version=2))
    assert exc.value.current_version == 3
    assert service.get_resource(r.id).version == 3


# ---------- 并发删除竞争 ----------

def test_concurrent_deletes_only_one_wins(service: ResourceService) -> None:
    r = _create(service, name="counter")
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        try:
            d = service.delete_resource(
                ResourceDelete(id=r.id, expected_version=1)
            )
            results[name] = ("committed", d.version)
        except VersionConflictError as exc:
            results[name] = (exc.code, exc.current_version)

    t1 = threading.Thread(target=worker, args=("w1",))
    t2 = threading.Thread(target=worker, args=("w2",))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(v[0] for v in results.values())
    logger.info("[TEST] concurrent delete results=%s", results)
    assert statuses == ["committed", "version_conflict"]
    final = service.get_resource(r.id, include_deleted=True)
    assert final.status is ResourceStatus.DELETED and final.version == 2


# ---------- 名字保留规则 ----------

def test_name_reserved_while_deleted_and_released_after_purge(
    service: ResourceService,
) -> None:
    r = _create(service, name="keep")
    service.delete_resource(ResourceDelete(id=r.id, expected_version=1))

    # 软删期间名字仍被占用：不能新建同名（保证恢复绝不撞名）
    with pytest.raises(UniqueConstraintError):
        service.create_resource(ResourceCreate(name="keep"))

    # 恢复后名字仍是原资源的
    service.restore_resource(ResourceRestore(id=r.id, expected_version=2))
    with pytest.raises(UniqueConstraintError):
        service.create_resource(ResourceCreate(name="keep"))

    # 物理清除后名字才可被新资源复用
    service.delete_resource(ResourceDelete(id=r.id, expected_version=3))
    service.purge_deleted(retention_ms=0)
    reused = service.create_resource(ResourceCreate(name="keep"))
    assert reused.id != r.id
    assert service.get_resource(reused.id).name == "keep"


def test_invalid_expected_version_rejected(service: ResourceService) -> None:
    r = _create(service)
    from app.errors import InvalidInputError

    with pytest.raises(InvalidInputError):
        service.delete_resource(ResourceDelete(id=r.id, expected_version=0))
    with pytest.raises(InvalidInputError):
        service.restore_resource(ResourceRestore(id=r.id, expected_version=-3))

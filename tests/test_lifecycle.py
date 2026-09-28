"""资源生命周期：软删除（回收站）与恢复。

约定：
- 删除/恢复都必须携带当前版本号做乐观锁，成功后版本号继续 +1，不回退不重复；
- 三类拒绝原因必须可区分：
  resource_not_found / version_conflict / invalid_state；
- 回收站资源在默认单查与默认列表中视为不存在，恢复后重新可见；
- 名字只被 active 资源占用：删除即释放，他人可复用；恢复时名字已被占用
  则以 unique_constraint_violation 拒绝，绝不产生重名。
"""
from __future__ import annotations

import logging
import threading

import pytest

from app.errors import (
    InvalidStateError,
    ResourceNotFoundError,
    UniqueConstraintError,
    VersionConflictError,
)
from app.models import (
    ResourceCreate,
    ResourceQuery,
    ResourceStateChange,
    ResourceUpdate,
)
from app.service import ResourceService

logger = logging.getLogger("app.dal")


def test_soft_delete_hides_resource_and_version_advances(
    service: ResourceService,
) -> None:
    created = service.create_resource(ResourceCreate(name="doc", payload="p0"))
    deleted = service.delete_resource(
        ResourceStateChange(id=created.id, expected_version=1)
    )
    assert deleted.status == "deleted"
    assert deleted.version == 2
    assert deleted.deleted_at is not None
    assert deleted.deleted_at == deleted.updated_at

    # 默认单查视为不存在
    with pytest.raises(ResourceNotFoundError):
        service.get_resource(created.id)
    # include_deleted 能看到回收站原貌
    ghost = service.get_resource(created.id, include_deleted=True)
    assert ghost.status == "deleted" and ghost.version == 2

    # 默认列表（只看 active）看不到；回收站列表能看到
    assert service.list_resources(ResourceQuery(page_size=10)).items == []
    trash = service.list_resources(
        ResourceQuery(status="deleted", page_size=10)
    ).items
    assert [i.id for i in trash] == [created.id]


def test_restore_makes_resource_visible_and_version_continues(
    service: ResourceService,
) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(ResourceStateChange(id=created.id, expected_version=1))
    restored = service.restore_resource(
        ResourceStateChange(id=created.id, expected_version=2)
    )
    assert restored.status == "active"
    assert restored.deleted_at is None
    assert restored.version == 3  # 版本号继续向前走：1 -> 2(删) -> 3(恢复)

    again = service.get_resource(created.id)
    assert again.version == 3
    # 恢复后还能继续正常更新
    updated = service.update_resource(
        ResourceUpdate(id=created.id, expected_version=3, payload="next")
    )
    assert updated.version == 4


def test_delete_missing_is_not_found(service: ResourceService) -> None:
    with pytest.raises(ResourceNotFoundError) as exc_info:
        service.delete_resource(
            ResourceStateChange(id="ghost", expected_version=1)
        )
    assert exc_info.value.code == "resource_not_found"


def test_restore_missing_is_not_found(service: ResourceService) -> None:
    with pytest.raises(ResourceNotFoundError) as exc_info:
        service.restore_resource(
            ResourceStateChange(id="ghost", expected_version=1)
        )
    assert exc_info.value.code == "resource_not_found"


def test_delete_stale_version_rejected(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    service.update_resource(
        ResourceUpdate(id=created.id, expected_version=1, payload="p1")
    )  # version -> 2
    with pytest.raises(VersionConflictError) as exc_info:
        service.delete_resource(
            ResourceStateChange(id=created.id, expected_version=1)
        )
    err = exc_info.value
    assert err.expected_version == 1 and err.current_version == 2
    # 拒绝后资源仍是正常状态、内容未被覆盖
    assert service.get_resource(created.id).payload == "p1"


def test_concurrent_restore_one_wins_other_sees_version_conflict(
    service: ResourceService,
) -> None:
    """两个并发恢复拿着同一版本抢同一回收站资源。

    写事务以 BEGIN IMMEDIATE 串行：胜者提交后版本号 2->3，败者拿到锁时
    自己手里的 v2 已经落后，因此得到 version_conflict（携带 current_version=3），
    而不是 invalid_state——状态确实适合恢复，真正的问题是版本落后。败者绝不会
    把已恢复资源再改一次，也不会产生重复版本。
    """
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(
        ResourceStateChange(id=created.id, expected_version=1)
    )  # v2
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(name: str) -> None:
        barrier.wait()
        try:
            service.restore_resource(
                ResourceStateChange(id=created.id, expected_version=2)
            )
            results[name] = "committed"
        except VersionConflictError as exc:
            results[name] = (VersionConflictError.code, exc.current_version)
        except InvalidStateError as exc:
            results[name] = (InvalidStateError.code, exc.current_status)

    t1 = threading.Thread(target=worker, args=("w1",))
    t2 = threading.Thread(target=worker, args=("w2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    logger.info("[TEST] concurrent restore results=%s", results)
    outcomes = sorted(
        v[0] if isinstance(v, tuple) else v for v in results.values()
    )
    assert outcomes == ["committed", "version_conflict"]
    loser_result = next(
        v for v in results.values() if isinstance(v, tuple)
    )
    assert loser_result == ("version_conflict", 3)
    final = service.get_resource(created.id)
    assert final.status == "active"
    assert final.version == 3  # 只被恢复一次，版本不重复


def test_concurrent_delete_same_version_loser_sees_version_conflict(
    service: ResourceService,
) -> None:
    """两个并发删除拿着同一版本抢同一正常资源：一成一版本冲突。

    败者读到的最新状态已是 deleted，但失败原因必须是 version_conflict
    （自己手里的 v1 落后了），而不是“状态不允许”。资源只被删一次。
    """
    created = service.create_resource(ResourceCreate(name="doc"))
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(name: str) -> None:
        barrier.wait()
        try:
            service.delete_resource(
                ResourceStateChange(id=created.id, expected_version=1)
            )
            results[name] = "committed"
        except VersionConflictError as exc:
            results[name] = (VersionConflictError.code, exc.current_version)
        except InvalidStateError as exc:
            results[name] = (InvalidStateError.code, exc.current_status)

    t1 = threading.Thread(target=worker, args=("w1",))
    t2 = threading.Thread(target=worker, args=("w2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    logger.info("[TEST] concurrent delete results=%s", results)
    outcomes = sorted(
        v[0] if isinstance(v, tuple) else v for v in results.values()
    )
    assert outcomes == ["committed", "version_conflict"]
    loser_result = next(v for v in results.values() if isinstance(v, tuple))
    assert loser_result == ("version_conflict", 2)
    ghost = service.get_resource(created.id, include_deleted=True)
    assert ghost.status == "deleted" and ghost.version == 2


def test_restore_with_current_version_after_delete_wins_is_version_conflict(
    service: ResourceService,
) -> None:
    """拿着刚读到的最新版本恢复，但提交前被别人抢先重新删除/恢复。

    败者的版本此刻已经落后 -> version_conflict；重读后拿新版本仍可继续操作。
    """
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(ResourceStateChange(id=created.id, expected_version=1))  # v2

    # 模拟“别人抢先恢复又重新删除”：deleted_at 刷新，版本走到 v4
    service.restore_resource(ResourceStateChange(id=created.id, expected_version=2))  # v3
    service.delete_resource(ResourceStateChange(id=created.id, expected_version=3))  # v4

    # 我还拿着 v2 想恢复（资源确实在回收站、状态适合），但版本落后
    with pytest.raises(VersionConflictError) as exc_info:
        service.restore_resource(
            ResourceStateChange(id=created.id, expected_version=2)
        )
    err = exc_info.value
    assert err.code == "version_conflict"
    assert err.expected_version == 2 and err.current_version == 4
    # 重读最新版本后恢复成功，证明冲突后状态可继续演进
    again = service.restore_resource(
        ResourceStateChange(id=created.id, expected_version=4)
    )
    assert again.status == "active" and again.version == 5


def test_restore_stale_version_rejected(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(
        ResourceStateChange(id=created.id, expected_version=1)
    )  # v2
    # 资源仍在回收站，但调用方拿着过期版本（v1）尝试恢复 => 版本冲突
    with pytest.raises(VersionConflictError) as exc_info:
        service.restore_resource(
            ResourceStateChange(id=created.id, expected_version=1)
        )
    err = exc_info.value
    assert err.expected_version == 1 and err.current_version == 2
    # 被拒绝后仍在回收站，未被误恢复
    assert (
        service.get_resource(created.id, include_deleted=True).status == "deleted"
    )


def test_delete_stale_version_when_already_deleted_is_conflict(
    service: ResourceService,
) -> None:
    """资源已在回收站，且调用方拿着落后版本再删一次：必须报版本冲突。

    虽然资源当前确实是 deleted（状态也不适合再删），但调用方真正需要知道
    的是“自己的版本落后了”；只有版本就是最新、状态仍不适合时才报
    invalid_state（见 test_double_delete_is_invalid_state）。
    """
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(
        ResourceStateChange(id=created.id, expected_version=1)
    )  # v1 -> v2
    service.restore_resource(
        ResourceStateChange(id=created.id, expected_version=2)
    )  # v2 -> v3
    service.delete_resource(
        ResourceStateChange(id=created.id, expected_version=3)
    )  # v3 -> v4，重新进回收站
    # 拿着 v1（既落后、资源又已是 deleted）删除 -> version_conflict
    with pytest.raises(VersionConflictError) as exc_info:
        service.delete_resource(
            ResourceStateChange(id=created.id, expected_version=1)
        )
    err = exc_info.value
    assert err.code == "version_conflict"
    assert err.expected_version == 1 and err.current_version == 4
    # 未被当成成功，状态/版本保持原样
    ghost = service.get_resource(created.id, include_deleted=True)
    assert ghost.status == "deleted" and ghost.version == 4


def test_double_delete_is_invalid_state(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(ResourceStateChange(id=created.id, expected_version=1))
    with pytest.raises(InvalidStateError) as exc_info:
        service.delete_resource(
            ResourceStateChange(id=created.id, expected_version=2)
        )
    err = exc_info.value
    assert err.code == "invalid_state"
    assert err.current_status == "deleted"


def test_restore_active_is_invalid_state(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    with pytest.raises(InvalidStateError) as exc_info:
        service.restore_resource(
            ResourceStateChange(id=created.id, expected_version=1)
        )
    assert exc_info.value.current_status == "active"


def test_update_deleted_resource_is_not_found(service: ResourceService) -> None:
    created = service.create_resource(ResourceCreate(name="doc"))
    service.delete_resource(ResourceStateChange(id=created.id, expected_version=1))
    with pytest.raises(ResourceNotFoundError):
        service.update_resource(
            ResourceUpdate(id=created.id, expected_version=2, payload="x")
        )


def test_deleted_name_can_be_reused(service: ResourceService) -> None:
    first = service.create_resource(ResourceCreate(name="shared"))
    service.delete_resource(ResourceStateChange(id=first.id, expected_version=1))
    # 同名新资源可以创建成功
    second = service.create_resource(ResourceCreate(name="shared"))
    assert second.id != first.id
    assert service.get_resource(second.id).name == "shared"


def test_restore_with_reused_name_conflicts(service: ResourceService) -> None:
    first = service.create_resource(ResourceCreate(name="shared"))
    service.delete_resource(ResourceStateChange(id=first.id, expected_version=1))
    service.create_resource(ResourceCreate(name="shared"))
    with pytest.raises(UniqueConstraintError) as exc_info:
        service.restore_resource(
            ResourceStateChange(id=first.id, expected_version=2)
        )
    assert exc_info.value.code == "unique_constraint_violation"
    # 恢复被拒绝：旧资源仍在回收站，未造成任何重名/覆盖
    assert (
        service.get_resource(first.id, include_deleted=True).status == "deleted"
    )
    actives = service.list_resources(ResourceQuery(page_size=10)).items
    assert [i.name for i in actives] == ["shared"]
    assert actives[0].id != first.id


def test_restore_succeeds_after_name_freed(service: ResourceService) -> None:
    first = service.create_resource(ResourceCreate(name="shared"))
    service.delete_resource(ResourceStateChange(id=first.id, expected_version=1))
    second = service.create_resource(ResourceCreate(name="shared"))
    # 占用者自己也被删除 -> 名字再次释放 -> 旧资源可恢复
    service.delete_resource(
        ResourceStateChange(id=second.id, expected_version=1)
    )
    restored = service.restore_resource(
        ResourceStateChange(id=first.id, expected_version=2)
    )
    assert restored.status == "active" and restored.name == "shared"

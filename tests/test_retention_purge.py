"""回收站超期清理测试。

覆盖：
- 仅清除超过保留期的记录，保留期内与恰好到点的记录不误伤（严格小于）；
- 分批推进：多批短事务，批次数与计数正确；
- 中途失败后重试结果一致：不重复清理、不把状态搞乱；
- 清理结果只含可解释字段，不暴露底层存储细节；
- 清理释放名字后可被复用，active 资源始终不受影响。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import InvalidInputError
from app.models import (
    ResourceCreate,
    ResourceDelete,
    ResourceQuery,
    ResourceStatus,
)
from app.service import ResourceService
from app.transaction import transactional
from app.repository import ResourceRepository

logger = logging.getLogger("app.dal")


@pytest.fixture
def clock():
    return {"t": 1_000_000}


@pytest.fixture
def timed_service(db, clock) -> ResourceService:
    return ResourceService(db, clock=lambda: clock["t"])


def _delete_at(timed_service: ResourceService, rid: str, version: int = 1) -> None:
    timed_service.delete_resource(
        ResourceDelete(id=rid, expected_version=version)
    )


def test_purge_only_removes_expired(timed_service: ResourceService, clock) -> None:
    # old: 删除于 t-100000；fresh: 删除于当前
    old = timed_service.create_resource(ResourceCreate(name="old"))
    clock["t"] = 900_000
    _delete_at(timed_service, old.id)

    fresh = timed_service.create_resource(ResourceCreate(name="fresh"))
    clock["t"] = 1_000_000
    _delete_at(timed_service, fresh.id)

    # 保留期 50_000：cutoff=950_000，old(900_000) 超期，fresh(1_000_000) 未到
    result = timed_service.purge_deleted(retention_ms=50_000, batch_size=100)
    assert result.purged_count == 1
    assert result.retention_ms == 50_000
    assert result.cutoff_ms == 950_000
    assert result.has_more is False

    from app.errors import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        timed_service.get_resource(old.id, include_deleted=True)
    still = timed_service.get_resource(fresh.id, include_deleted=True)
    assert still.status is ResourceStatus.DELETED


def test_boundary_exactly_at_retention_is_kept(
    timed_service: ResourceService, clock
) -> None:
    r = timed_service.create_resource(ResourceCreate(name="edge"))
    clock["t"] = 1_000_000
    _delete_at(timed_service, r.id)
    # now == deleted_at + retention，cutoff == deleted_at：严格小于，保留
    result = timed_service.purge_deleted(
        retention_ms=60_000, batch_size=10, now_ms=1_060_000
    )
    assert result.purged_count == 0
    assert timed_service.get_resource(r.id, include_deleted=True).id == r.id
    # 一毫秒之后即被清除
    result2 = timed_service.purge_deleted(
        retention_ms=60_000, batch_size=10, now_ms=1_060_001
    )
    assert result2.purged_count == 1


def test_purge_progresses_in_batches(timed_service: ResourceService, clock) -> None:
    for i in range(5):
        r = timed_service.create_resource(ResourceCreate(name=f"d{i}"))
        clock["t"] = 900_000 + i  # 删除时间递增，验证分批顺序稳定
        _delete_at(timed_service, r.id)
    clock["t"] = 2_000_000

    result = timed_service.purge_deleted(retention_ms=10_000, batch_size=2)
    assert result.purged_count == 5
    assert result.batch_count == 3  # 2 + 2 + 1
    trash = timed_service.list_resources(
        ResourceQuery(page_size=100, status=ResourceStatus.DELETED)
    ).items
    assert trash == []


def test_purge_retry_is_consistent(timed_service: ResourceService, clock) -> None:
    ids = []
    for i in range(4):
        r = timed_service.create_resource(ResourceCreate(name=f"r{i}"))
        clock["t"] = 900_000
        _delete_at(timed_service, r.id)
        ids.append(r.id)
    clock["t"] = 2_000_000

    # 第一批成功删除 2 条后，模拟进程在中途失败（只跑一个手动批事务）
    with transactional(timed_service.db) as conn:
        deleted, has_more = ResourceRepository(conn).purge_expired(
            cutoff_ms=1_990_000, batch_size=2
        )
    assert deleted == 2 and has_more is True

    # 用同样的截止时间重试整轮：只剩 2 条，已删的不会重复处理
    result = timed_service.purge_deleted(
        retention_ms=10_000, batch_size=2, now_ms=2_000_000
    )
    assert result.purged_count == 2
    # 再次重试：幂等，0 条，状态不乱
    again = timed_service.purge_deleted(
        retention_ms=10_000, batch_size=2, now_ms=2_000_000
    )
    assert again.purged_count == 0
    from app.errors import ResourceNotFoundError

    for rid in ids:
        with pytest.raises(ResourceNotFoundError):
            timed_service.get_resource(rid, include_deleted=True)


def test_purge_keeps_active_untouched(timed_service: ResourceService, clock) -> None:
    active = timed_service.create_resource(ResourceCreate(name="alive"))
    gone = timed_service.create_resource(ResourceCreate(name="gone"))
    clock["t"] = 0
    _delete_at(timed_service, gone.id)
    clock["t"] = 10_000_000

    result = timed_service.purge_deleted(retention_ms=0, batch_size=10)
    assert result.purged_count == 1
    assert timed_service.get_resource(active.id).id == active.id
    # active 列表数量与内容不变
    items = timed_service.list_resources(
        ResourceQuery(page_size=100, status=ResourceStatus.ACTIVE)
    ).items
    assert [i.id for i in items] == [active.id]


def test_purge_releases_name_for_reuse(timed_service: ResourceService, clock) -> None:
    r = timed_service.create_resource(ResourceCreate(name="recycle-me"))
    clock["t"] = 0
    _delete_at(timed_service, r.id)
    clock["t"] = 1_000
    timed_service.purge_deleted(retention_ms=10, batch_size=10)  # 0 < 990 超期
    new = timed_service.create_resource(ResourceCreate(name="recycle-me"))
    assert new.id != r.id


def test_purge_result_is_explainable(timed_service: ResourceService, clock) -> None:
    result = timed_service.purge_deleted(retention_ms=1, batch_size=1)
    # 对外结果是干净的数据对象，不含 SQL/连接等存储细节
    assert set(
        ("purged_count", "batch_count", "cutoff_ms", "retention_ms", "has_more")
    ) <= set(result.__dataclass_fields__)
    assert result.purged_count == 0 and result.batch_count == 1


@pytest.mark.parametrize(
    "retention,batch",
    [(-1, 10), (0, 0), (100, -5)],
)
def test_purge_invalid_arguments(
    timed_service: ResourceService, retention: int, batch: int
) -> None:
    with pytest.raises(InvalidInputError):
        timed_service.purge_deleted(retention_ms=retention, batch_size=batch)

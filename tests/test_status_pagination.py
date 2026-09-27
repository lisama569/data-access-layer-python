"""带状态筛选的游标分页稳定性测试。

排序键仍是固定的 (created_at ASC, id ASC)，游标只表示排序位置、与筛选无关。
验证翻页过程中发生 删除（移入回收站）/ 恢复 / 新增 / 物理清理 时：
- 不重复、不遗漏本应属于该筛选集合的记录；
- 状态筛选之间互不串扰；
- 非法游标明确拒绝，越过末尾的游标给出干净结束信号。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import InvalidCursorError
from app.models import (
    ResourceCreate,
    ResourceDelete,
    ResourceQuery,
    ResourceRestore,
    ResourceStatus,
)
from app.repository import ResourceRepository
from app.service import ResourceService
from app.transaction import transactional

logger = logging.getLogger("app.dal")


def _seed_mixed(db, service: ResourceService, n: int = 9, step: int = 10):
    """row-000..row-008 全部 active，时间戳 0,10,...；返回 id 列表。"""
    ids = [f"row-{i:03d}" for i in range(n)]
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        for i, rid in enumerate(ids):
            repo.insert(ResourceCreate(name=f"name-{i:03d}"), now_ms=i * step, resource_id=rid)
    return ids


def _walk(service: ResourceService, page_size: int, status: ResourceStatus, *, mutator=None):
    query = ResourceQuery(page_size=page_size, status=status)
    seen: list[str] = []
    mutated = False
    while True:
        page = service.list_resources(query)
        seen.extend(i.id for i in page.items)
        # 具体状态筛选时返回项必须符合该状态；ALL 视图包含两种状态，不校验
        if status is not ResourceStatus.ALL:
            assert all(i.status is status for i in page.items), "结果混入了其它状态的记录"
        if not page.has_more:
            break
        if mutator is not None and not mutated:
            mutator()
            mutated = True
        query = ResourceQuery(page_size=page_size, status=status, cursor=page.next_cursor)
    return seen


def test_active_filter_excludes_deleted(service: ResourceService, db) -> None:
    ids = _seed_mixed(db, service)
    # 删掉 row-002 / row-005
    for rid in ("row-002", "row-005"):
        service.delete_resource(
            ResourceDelete(id=rid, expected_version=1)
        )
    active = _walk(service, 3, ResourceStatus.ACTIVE)
    assert active == [i for i in ids if i not in {"row-002", "row-005"}]

    deleted = _walk(service, 3, ResourceStatus.DELETED)
    assert deleted == ["row-002", "row-005"]

    all_rows = _walk(service, 3, ResourceStatus.ALL)
    assert all_rows == ids  # 全量视图包含两种状态，顺序不变


def test_soft_delete_ahead_during_active_walk(service: ResourceService, db) -> None:
    ids = _seed_mixed(db, service)

    def mutate() -> None:
        # 翻页中删除：锚点 row-002、即将读到的 row-003、更后面的 row-006
        for rid, ver in (("row-002", 1), ("row-003", 1), ("row-006", 1)):
            service.delete_resource(ResourceDelete(id=rid, expected_version=ver))

    seen = _walk(service, 3, ResourceStatus.ACTIVE, mutator=mutate)
    assert len(seen) == len(set(seen)), "active 遍历时出现重复"
    assert "row-003" not in seen and "row-006" not in seen
    survivors = [i for i in ids if i not in {"row-002", "row-003", "row-006"}]
    assert "row-002" in seen  # 删除前已在第一页读到，保留合法
    assert set(survivors) <= set(seen)
    assert [i for i in seen if i in survivors] == survivors, "存活记录乱序"

    # 同一时刻回收站视图应能稳定读到被删的 3 条
    trash = _walk(service, 3, ResourceStatus.DELETED)
    assert sorted(trash) == ["row-002", "row-003", "row-006"]


def test_restore_ahead_during_deleted_walk(service: ResourceService, db) -> None:
    ids = _seed_mixed(db, service)
    for rid in ("row-001", "row-003", "row-006", "row-007"):
        service.delete_resource(ResourceDelete(id=rid, expected_version=1))

    def mutate() -> None:
        # 翻回收站时恢复：锚点(row-003)、后面的(row-007)
        service.restore_resource(ResourceRestore(id="row-003", expected_version=2))
        service.restore_resource(ResourceRestore(id="row-007", expected_version=2))

    deleted = _walk(service, 2, ResourceStatus.DELETED, mutator=mutate)
    assert len(deleted) == len(set(deleted)), "回收站遍历出现重复"
    assert "row-007" not in deleted
    assert "row-001" in deleted and "row-006" in deleted
    # 被恢复的两条回到 active 列表
    active = _walk(service, 100, ResourceStatus.ACTIVE)
    assert "row-003" in active and "row-007" in active


def test_insert_during_filtered_walk_read_once(service: ResourceService, db) -> None:
    ids = _seed_mixed(db, service)

    def mutate() -> None:
        with transactional(db) as conn:
            ResourceRepository(conn).insert(
                ResourceCreate(name="late"), now_ms=95, resource_id="row-late"
            )

    active = _walk(service, 3, ResourceStatus.ACTIVE, mutator=mutate)
    assert active.count("row-late") == 1
    assert len(active) == len(set(active))
    assert set(ids) <= set(active)

    # 新插入的是 active，回收站视图不应出现
    assert "row-late" not in _walk(service, 100, ResourceStatus.DELETED)


def test_purge_during_deleted_walk(service: ResourceService, db) -> None:
    ids = _seed_mixed(db, service)
    for rid in ("row-001", "row-004", "row-007"):
        service.delete_resource(ResourceDelete(id=rid, expected_version=1))

    def mutate() -> None:
        # 物理清除（默认保留期 0 即清全部回收站）
        result = service.purge_deleted(retention_ms=0)
        logger.info("[TEST] purge during walk purged=%d", result.purged_count)
        assert result.purged_count == 3

    deleted = _walk(service, 2, ResourceStatus.DELETED, mutator=mutate)
    # 已物理删除的行绝不重复出现；遍历干净终止
    assert len(deleted) == len(set(deleted))
    assert set(deleted) <= {"row-001", "row-004", "row-007"}
    # 再查回收站应为空
    assert _walk(service, 100, ResourceStatus.DELETED) == []
    # 其它 active 记录不受影响
    active = _walk(service, 100, ResourceStatus.ACTIVE)
    assert active == [i for i in ids if i not in {"row-001", "row-004", "row-007"}]


def test_same_cursor_stable_across_filters(service: ResourceService, db) -> None:
    ids = _seed_mixed(db, service)
    service.delete_resource(ResourceDelete(id="row-004", expected_version=1))

    first = service.list_resources(ResourceQuery(page_size=3, status=ResourceStatus.ACTIVE))
    cursor = first.next_cursor
    # 同一游标在 active 与 all 下各自稳定、可重复
    a1 = service.list_resources(ResourceQuery(page_size=3, status=ResourceStatus.ACTIVE, cursor=cursor))
    a2 = service.list_resources(ResourceQuery(page_size=3, status=ResourceStatus.ACTIVE, cursor=cursor))
    assert [i.id for i in a1.items] == [i.id for i in a2.items]
    assert all(i.status is ResourceStatus.ACTIVE for i in a1.items)

    l1 = service.list_resources(ResourceQuery(page_size=3, status=ResourceStatus.ALL, cursor=cursor))
    l2 = service.list_resources(ResourceQuery(page_size=3, status=ResourceStatus.ALL, cursor=cursor))
    assert [i.id for i in l1.items] == [i.id for i in l2.items]
    # all 在该位置之后能看到 row-004，active 不能
    assert "row-004" in [i.id for i in l1.items]
    assert "row-004" not in [i.id for i in a1.items]


@pytest.mark.parametrize("bad", ["", "@@@", "not-a-cursor"])
def test_invalid_cursor_with_filter_rejected(
    service: ResourceService, db, bad: str
) -> None:
    _seed_mixed(db, service)
    with pytest.raises(InvalidCursorError):
        service.list_resources(
            ResourceQuery(page_size=3, status=ResourceStatus.DELETED, cursor=bad)
        )


def test_cursor_past_end_per_filter_terminates(service: ResourceService, db) -> None:
    _seed_mixed(db, service)
    from app.cursor import encode_cursor

    token = encode_cursor(9_999_999, "zzz")
    for status in (ResourceStatus.ACTIVE, ResourceStatus.DELETED, ResourceStatus.ALL):
        page = service.list_resources(
            ResourceQuery(page_size=5, status=status, cursor=token)
        )
        assert page.items == [] and page.has_more is False and page.next_cursor is None


def test_invalid_status_value_rejected(service: ResourceService) -> None:
    from app.errors import InvalidInputError

    # 绕过 StrEnum 直接构造非法字符串（HTTP 层也会拦）
    q = ResourceQuery(page_size=5, status="archived")  # type: ignore[arg-type]
    with pytest.raises(InvalidInputError):
        service.list_resources(q)

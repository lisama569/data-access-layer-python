"""带资源状态筛选的游标分页。

排序键仍是 (created_at ASC, id ASC)，游标仍是“排序位置”；状态筛选只影响
可见行集合，不改变游标编码。因此在一轮翻页过程中发生删除/恢复/新增/物理
清理时：
- 仍在结果集合里的行不重复、不遗漏、不乱序；
- 被移出结果集合的行（删除/清理）若此前页未读到则不再出现；
- 被移入结果集合的行（恢复）位置在当前游标之后则会读到、恰好一次；
- 位置在当前游标之前的变动不回溯；
- 非法/过期游标与原分页语义一致。
"""
from __future__ import annotations

import logging

from app.models import ResourceCreate, ResourceQuery, ResourceStateChange
from app.repository import ResourceRepository
from app.service import ResourceService
from app.transaction import transactional

logger = logging.getLogger("app.dal")


def _seed(service: ResourceService, db, n: int, step: int = 10) -> list[str]:
    ids = [f"row-{i:03d}" for i in range(n)]
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        for i, rid in enumerate(ids):
            repo.insert(
                ResourceCreate(name=f"name-{i:03d}"),
                now_ms=i * step,
                resource_id=rid,
            )
    return ids


def _walk(
    service: ResourceService,
    page_size: int,
    *,
    status: str,
    mutator=None,
) -> list[str]:
    query = ResourceQuery(page_size=page_size, status=status)
    seen: list[str] = []
    mutated = False
    while True:
        page = service.list_resources(query)
        seen.extend(i.id for i in page.items)
        if not page.has_more:
            break
        if mutator is not None and not mutated:
            mutator()
            mutated = True
        query = ResourceQuery(
            page_size=page_size, status=status, cursor=page.next_cursor
        )
    return seen


def test_default_list_excludes_deleted(service: ResourceService, db) -> None:
    ids = _seed(service, db, 6)
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        repo.soft_delete(
            ResourceStateChange(id="row-001", expected_version=1), now_ms=100
        )
        repo.soft_delete(
            ResourceStateChange(id="row-004", expected_version=1), now_ms=100
        )
    default_ids = [i.id for i in service.list_resources(
        ResourceQuery(status="active", page_size=50)
    ).items]
    assert default_ids == [i for i in ids if i not in {"row-001", "row-004"}]
    trash_ids = [i.id for i in service.list_resources(
        ResourceQuery(status="deleted", page_size=50)
    ).items]
    assert trash_ids == ["row-001", "row-004"]  # 排序键仍是 created_at,id
    all_ids = [i.id for i in service.list_resources(
        ResourceQuery(status="all", page_size=50)
    ).items]
    assert all_ids == ids


def test_delete_during_active_walk(service: ResourceService, db) -> None:
    ids = _seed(service, db, 10)

    def mutate() -> None:
        with transactional(db) as conn:
            ResourceRepository(conn).soft_delete(
                ResourceStateChange(id="row-003", expected_version=1), now_ms=200
            )
            ResourceRepository(conn).soft_delete(
                ResourceStateChange(id="row-006", expected_version=1), now_ms=200
            )

    seen = _walk(service, page_size=3, status="active", mutator=mutate)
    assert len(seen) == len(set(seen)), "出现重复"
    survivors = [i for i in ids if i not in {"row-003", "row-006"}]
    assert "row-003" not in seen and "row-006" not in seen
    assert set(survivors) <= set(seen)
    assert [i for i in seen if i in survivors] == survivors


def test_restore_during_deleted_walk(service: ResourceService, db) -> None:
    """遍历回收站时，有资源被恢复（移出回收站）：尚未读到的不再出现。"""
    ids = _seed(service, db, 10)
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        for i in range(10):
            repo.soft_delete(
                ResourceStateChange(id=f"row-{i:03d}", expected_version=1),
                now_ms=1000 + i,
            )

    def mutate() -> None:
        with transactional(db) as conn:
            # 恢复两个位置靠后的回收站资源
            ResourceRepository(conn).restore(
                ResourceStateChange(id="row-005", expected_version=2), now_ms=2000
            )
            ResourceRepository(conn).restore(
                ResourceStateChange(id="row-008", expected_version=2), now_ms=2000
            )

    seen = _walk(service, page_size=3, status="deleted", mutator=mutate)
    assert len(seen) == len(set(seen))
    assert "row-005" not in seen and "row-008" not in seen
    assert set(ids) - {"row-005", "row-008"} <= set(seen)
    # 被恢复的资源现在确实在 active 视图
    active = {
        i.id
        for i in service.list_resources(
            ResourceQuery(status="active", page_size=50)
        ).items
    }
    assert {"row-005", "row-008"} <= active


def test_restore_ahead_of_active_walk_is_read_once(
    service: ResourceService, db
) -> None:
    """遍历正常列表时，位置在当前游标之后的资源被恢复，会读到且恰好一次。"""
    ids = _seed(service, db, 10)
    # 先把 row-007 删了，active 视图不含它
    with transactional(db) as conn:
        ResourceRepository(conn).soft_delete(
            ResourceStateChange(id="row-007", expected_version=1), now_ms=500
        )

    def mutate() -> None:
        with transactional(db) as conn:
            ResourceRepository(conn).restore(
                ResourceStateChange(id="row-007", expected_version=2), now_ms=600
            )

    seen = _walk(service, page_size=3, status="active", mutator=mutate)
    assert seen.count("row-007") == 1
    assert len(seen) == len(set(seen))
    assert set(ids) <= set(seen)


def test_restore_behind_active_walk_not_backfilled(
    service: ResourceService, db
) -> None:
    """资源在已翻过的区间被恢复：本轮不回溯，下一轮从头可见。"""
    _seed(service, db, 10)
    with transactional(db) as conn:
        ResourceRepository(conn).soft_delete(
            ResourceStateChange(id="row-001", expected_version=1), now_ms=500
        )

    def mutate() -> None:
        with transactional(db) as conn:
            ResourceRepository(conn).restore(
                ResourceStateChange(id="row-001", expected_version=2), now_ms=600
            )

    seen = _walk(service, page_size=3, status="active", mutator=mutate)
    assert "row-001" not in seen
    assert len(seen) == len(set(seen))
    fresh = _walk(service, page_size=100, status="active")
    assert "row-001" in fresh


def test_purge_during_deleted_walk(service: ResourceService, db) -> None:
    """遍历回收站时，部分行被物理清理，仍然不重不漏地干净终止。"""
    ids = _seed(service, db, 10)
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        for i in range(10):
            repo.soft_delete(
                ResourceStateChange(id=f"row-{i:03d}", expected_version=1),
                now_ms=1000 + i,
            )

    def mutate() -> None:
        with transactional(db) as conn:
            conn.execute(
                "DELETE FROM resources WHERE id IN ('row-004','row-007')"
            )

    seen = _walk(service, page_size=3, status="deleted", mutator=mutate)
    assert len(seen) == len(set(seen))
    assert "row-004" not in seen and "row-007" not in seen
    assert set(ids) - {"row-004", "row-007"} <= set(seen)


def test_invalid_status_rejected(service: ResourceService) -> None:
    from app.errors import InvalidInputError

    import pytest

    with pytest.raises(InvalidInputError):
        service.list_resources(ResourceQuery(status="archived", page_size=5))


def test_filtered_walk_terminates_with_has_more_false(
    service: ResourceService, db
) -> None:
    _seed(service, db, 7)
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        for i in range(7):
            repo.soft_delete(
                ResourceStateChange(id=f"row-{i:03d}", expected_version=1),
                now_ms=100 + i,
            )
    # 回收站为空过滤后仍以 has_more=False 终止（这里全部在回收站，active 为空）
    page = service.list_resources(ResourceQuery(status="active", page_size=3))
    assert page.items == [] and page.has_more is False and page.next_cursor is None


def test_cursor_from_other_status_filter_is_still_a_valid_position(
    service: ResourceService, db
) -> None:
    """游标只编码排序位置，不绑定筛选条件。

    它仍指向 (created_at, id) 空间中的同一位置，因此跨筛选复用时游标本身
    不会被判非法，只是按新的状态集合从该位置继续——结果确定、不报错。
    """
    from app.cursor import encode_cursor

    _seed(service, db, 10)
    with transactional(db) as conn:
        ResourceRepository(conn).soft_delete(
            ResourceStateChange(id="row-005", expected_version=1), now_ms=500
        )
    # 从 active 视图第一页拿到一个位置游标
    first = service.list_resources(
        ResourceQuery(status="active", page_size=3)
    )
    cursor = first.next_cursor
    # 用同一游标去查 deleted 视图：合法，返回的行都在该位置之后且为回收站
    deleted_page = service.list_resources(
        ResourceQuery(status="deleted", page_size=10, cursor=cursor)
    )
    # 回收站中只有 row-005（ts=50），位于第一页游标位置（ts=20,row-002）之后
    assert [i.id for i in deleted_page.items] == ["row-005"]
    # 乱造的游标依旧被明确拒绝
    from app.errors import InvalidCursorError
    import pytest

    with pytest.raises(InvalidCursorError):
        service.list_resources(
            ResourceQuery(status="deleted", page_size=5, cursor="@@@bad@@@")
        )


def test_name_contains_combined_with_status(service: ResourceService, db) -> None:
    _seed(service, db, 10)
    with transactional(db) as conn:
        repo = ResourceRepository(conn)
        # row-001 含子串 "1"，删除它
        repo.soft_delete(
            ResourceStateChange(id="row-001", expected_version=1), now_ms=500
        )
    active_match = [
        i.id
        for i in service.list_resources(
            ResourceQuery(status="active", name_contains="name-00", page_size=50)
        ).items
    ]
    # name-000..name-009 都匹配 "name-00"，但 active 视图排除 row-001
    assert "row-001" not in active_match
    assert "row-000" in active_match and "row-009" in active_match
    deleted_match = [
        i.id
        for i in service.list_resources(
            ResourceQuery(status="deleted", name_contains="name-00", page_size=50)
        ).items
    ]
    assert deleted_match == ["row-001"]

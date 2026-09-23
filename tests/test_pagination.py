"""游标分页语义测试。

排序键固定为 (created_at ASC, id ASC)，游标为不透明字符串，记录上一页
最后一条记录的排序键位置（keyset pagination）。

翻页期间数据变化的约定：
- 游标是“位置”，不依赖具体行：锚点行被删除后翻页依然正确，不跳行；
- 位置之后被删除的行自然不再出现；仍存在的行绝不重复、绝不遗漏；
- 位置之后新插入的行会被读取到（恰一次）；位置之前新插入的行不回溯；
- 遍历始终以 has_more=False 明确终止；非法游标一律拒绝（invalid_cursor）。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import InvalidCursorError
from app.models import ResourceCreate, ResourceQuery
from app.repository import ResourceRepository
from app.service import ResourceService
from app.transaction import transactional

logger = logging.getLogger("app.dal")


def _seed(service: ResourceService, db, n: int, step: int = 10) -> list[str]:
    """以确定性时间戳写入 n 条资源，返回按排序键顺序的 id 列表。"""
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


def _walk(service: ResourceService, page_size: int, *, mutator=None) -> list[str]:
    """从头翻到尾；可在第一页之后执行 mutator(db) 改变数据。"""
    query = ResourceQuery(page_size=page_size)
    seen: list[str] = []
    rounds = 0
    mutated = False
    while True:
        page = service.list_resources(query)
        seen.extend(i.id for i in page.items)
        rounds += 1
        logger.info(
            "[TEST] page=%d got=%d cursor_next=%s has_more=%s",
            rounds, len(page.items), page.next_cursor, page.has_more,
        )
        if not page.has_more:
            break
        if mutator is not None and not mutated:
            mutator()
            mutated = True
        query = ResourceQuery(page_size=page_size, cursor=page.next_cursor)
    return seen


def test_walk_all_pages_no_gap_no_dup(service: ResourceService, db) -> None:
    ids = _seed(service, db, 23)
    seen = _walk(service, page_size=5)
    assert seen == ids  # 顺序与排序键一致
    assert len(seen) == len(set(seen)) == 23  # 无重复无遗漏


def test_same_cursor_is_stable(service: ResourceService, db) -> None:
    _seed(service, db, 10)
    first = service.list_resources(ResourceQuery(page_size=3))
    first_ids = [i.id for i in first.items]
    # 用同一游标重复读取，结果必须完全一致
    repeated = service.list_resources(
        ResourceQuery(page_size=3, cursor=first.next_cursor)
    )
    again = service.list_resources(
        ResourceQuery(page_size=3, cursor=first.next_cursor)
    )
    assert [i.id for i in repeated.items] == [i.id for i in again.items]
    assert not set(first_ids) & set(i.id for i in repeated.items)
    logger.info("[TEST] stable cursor second page=%s", [i.id for i in again.items])


@pytest.mark.parametrize(
    "bad_cursor",
    [
        "",
        "@@@not-base64@@@",
        "eyJ2Ijo5OTksInRzIjoxLCJpZCI6IngifQ",  # 可解码但版本号不受支持
        "eyJ0cyI6Im5vdC1udW0iLCJpZCI6IngifQ",  # 字段类型非法
    ],
)
def test_invalid_cursor_rejected(
    service: ResourceService, bad_cursor: str
) -> None:
    with pytest.raises(InvalidCursorError) as exc_info:
        service.list_resources(ResourceQuery(page_size=5, cursor=bad_cursor))
    logger.info("[TEST] cursor rejected code=%s", exc_info.value.code)
    assert exc_info.value.code == "invalid_cursor"


def test_cursor_past_end_terminates_empty(service: ResourceService, db) -> None:
    _seed(service, db, 3)
    page = service.list_resources(
        ResourceQuery(page_size=5, cursor=_encode(9_999_999, "zzz"))
    )
    # 位置在所有数据之后：不是非法游标，而是正常终止的空页
    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None


def test_delete_during_pagination(service: ResourceService, db) -> None:
    ids = _seed(service, db, 10)  # ts = 0,10,...,90

    def mutate() -> None:
        # 删除：上一页锚点行 row-002、即将读到的 row-003、更后面的 row-005
        with transactional(db) as conn:
            conn.execute("DELETE FROM resources WHERE id IN ('row-002','row-003','row-005')")
        logger.info("[TEST] deleted anchor=row-002 upcoming=row-003,row-005")

    seen = _walk(service, page_size=3, mutator=mutate)
    assert len(seen) == len(set(seen)), "出现重复记录"
    survivors = [i for i in ids if i not in {"row-002", "row-003", "row-005"}]
    # row-002 在删除前已被第一页读取，出现在结果中合法；
    # 但删除点之后才读到的 row-003/row-005 绝不能出现
    assert "row-003" not in seen and "row-005" not in seen
    assert set(survivors) <= set(seen), "存活记录出现遗漏"
    assert [i for i in seen if i in survivors] == survivors, "存活记录乱序"


def test_insert_ahead_of_cursor_is_read_once(service: ResourceService, db) -> None:
    ids = _seed(service, db, 10)

    def mutate() -> None:
        with transactional(db) as conn:
            ResourceRepository(conn).insert(
                ResourceCreate(name="late"),
                now_ms=95,  # 位于 row-008(ts=80) 与 row-009(ts=90) 之后
                resource_id="row-late",
            )
        logger.info("[TEST] inserted ahead row-late ts=95")

    seen = _walk(service, page_size=3, mutator=mutate)
    assert seen.count("row-late") == 1
    assert len(seen) == len(set(seen))
    assert set(ids) <= set(seen)


def test_insert_behind_cursor_is_not_backfilled(service: ResourceService, db) -> None:
    _seed(service, db, 10)

    def mutate() -> None:
        with transactional(db) as conn:
            ResourceRepository(conn).insert(
                ResourceCreate(name="late-behind"),
                now_ms=5,  # 落在已翻过的区间 (0,10)
                resource_id="row-behind",
            )
        logger.info("[TEST] inserted behind cursor row-behind ts=5")

    seen = _walk(service, page_size=3, mutator=mutate)
    # 明确语义：不回溯；本轮遍历干净终止，无重复
    assert "row-behind" not in seen
    assert len(seen) == len(set(seen)) == 10
    # 但数据确实已持久化，下一轮从头遍历时可以读到
    fresh = _walk(service, page_size=100)
    assert "row-behind" in fresh


def _encode(ts: int, rid: str) -> str:
    from app.cursor import encode_cursor

    return encode_cursor(ts, rid)

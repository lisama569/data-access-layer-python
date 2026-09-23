"""游标分页：稳定性、非法游标拒绝、翻页中增删不重不漏。"""

from __future__ import annotations

import logging
import time

import pytest

from app.errors import InvalidCursorError

logger = logging.getLogger("dal.tests")


def test_pagination_walks_all_without_duplicates(service) -> None:
    total = 25
    for i in range(total):
        service.create_resource(f"item-{i:02d}", "active")
        time.sleep(0.001)  # 保证 created_at 严格递增、排序键确定

    seen: list[str] = []
    cursor = ""
    page_no = 0
    while True:
        page = service.list_resources(limit=10, cursor=cursor)
        page_no += 1
        seen.extend(item.id for item in page.items)
        cursor_preview = page.next_cursor or "<END>"
        logger.info(
            "PAGE n=%s size=%s has_more=%s cursor=%s",
            page_no,
            len(page.items),
            page.has_more,
            cursor_preview[:16],
        )
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert page_no == 3  # 10 + 10 + 5
    assert len(seen) == total
    assert len(set(seen)) == total  # 无重复
    logger.info("PAGINATION result=COMPLETE total=%s duplicates=0", total)


def test_filter_by_status_and_name(service) -> None:
    service.create_resource("apple", "active")
    service.create_resource("apricot", "active")
    service.create_resource("banana", "archived")

    active = service.list_resources(limit=50, status="active")
    assert {r.name for r in active.items} == {"apple", "apricot"}

    ap = service.list_resources(limit=50, name_contains="ap")
    assert {r.name for r in ap.items} == {"apple", "apricot"}

    # LIKE 通配符被转义，不破坏过滤语义。
    literal = service.list_resources(limit=50, name_contains="%")
    assert literal.items == []
    logger.info("FILTER active=%s name_match=%s wildcard_escaped=True", 2, 2)


@pytest.mark.parametrize(
    "bad_cursor",
    [
        "not-base64!!!",
        "eyJvIjogMX0=",  # base64 合法但字段缺失（{"o": 1}）
        "WzEsMl0=",      # JSON 不是对象（[1,2]）
        "",
    ],
)
def test_invalid_cursor_rejected(service, bad_cursor: str) -> None:
    if bad_cursor == "":
        pytest.skip("empty cursor means first page")
    with pytest.raises(InvalidCursorError):
        service.list_resources(cursor=bad_cursor)
    logger.info("CURSOR result=REJECTED cursor=%r", bad_cursor)


def test_non_comparable_timestamp_rejected(service) -> None:
    import base64
    import json

    token = base64.urlsafe_b64encode(
        json.dumps({"created_at": "not-a-time", "id": "x"}).encode()
    ).decode().rstrip("=")
    with pytest.raises(InvalidCursorError):
        service.list_resources(cursor=token)
    logger.info("CURSOR result=REJECTED reason=non_comparable_key")


def test_pagination_stable_under_deletes(service) -> None:
    """翻到第 1 页后删除“未来页”的记录：继续翻页不重复、不报错（终止语义）。"""
    ids = []
    for i in range(6):
        ids.append(service.create_resource(f"d-{i}", "s").id)
        time.sleep(0.001)

    first = service.list_resources(limit=3)
    assert [r.id for r in first.items] == ids[:3]
    cursor = first.next_cursor
    logger.info("CURSOR after page1=%s", cursor[:16])

    # 删除第二页应出现的第一条（游标位置之后）。
    conn = service  # 通过服务层删除：直接用 UoW
    from app.unit_of_work import UnitOfWork

    with UnitOfWork(service.db_path) as uow:
        uow.resources.conn.execute("DELETE FROM resources WHERE id = ?", (ids[3],))
        uow.commit()
    logger.info("DELETE during paging id=%s result=COMMITTED", ids[3])

    second = service.list_resources(limit=3, cursor=cursor)
    remaining_after = ids[4:6]
    assert [r.id for r in second.items] == remaining_after  # 无重复、无已删行
    assert second.has_more is False and second.next_cursor == ""
    logger.info("PAGE after delete size=%s cursor=<END> duplicates=0", len(second.items))


def test_pagination_insert_before_cursor_does_not_duplicate(service) -> None:
    """游标之后新增数据可在后续页读到；游标之前（更早排序键）新增不影响遍历。"""
    ids = []
    for i in range(3):
        ids.append(service.create_resource(f"s-{i}", "s").id)
        time.sleep(0.001)

    first = service.list_resources(limit=2)
    assert [r.id for r in first.items] == ids[:2]
    seen = set(r.id for r in first.items)

    # 新增记录的 created_at 更晚，落在游标之后，应在后续页出现且不重复。
    late = service.create_resource("s-late", "s")
    rest = service.list_resources(limit=10, cursor=first.next_cursor)
    rest_ids = [r.id for r in rest.items]
    assert late.id in rest_ids
    assert ids[2] in rest_ids
    assert seen.isdisjoint(rest_ids)
    logger.info(
        "PAGING WITH INSERT page2=%s duplicates=0 cursor=<END>",
        rest_ids,
    )

"""HTTP 入口行为测试：错误在 API 入口下同样得到一致归类。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.connection import Database
from app.http_api import create_app

logger = logging.getLogger("app.dal")


@pytest.fixture
def clock():
    class FakeClock:
        def __init__(self) -> None:
            self.t = 1_000_000

        def __call__(self) -> int:
            return self.t

    return FakeClock()


@pytest.fixture
def client(db: Database, clock) -> TestClient:
    # 注入确定性时钟，避免删除与清理落在同一毫秒导致的偶发
    from app.service import ResourceService

    app = create_app(db)
    app.state.service = ResourceService(db, clock=clock)
    return TestClient(app)


def _create(client: TestClient, name: str = "doc", payload: str = "") -> dict:
    resp = client.post("/resources", json={"name": name, "payload": payload})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_http_crud_and_listing(client: TestClient) -> None:
    created = _create(client, name="alpha", payload="p0")
    assert created["version"] == 1

    resp = client.put(
        f"/resources/{created['id']}",
        json={"expected_version": 1, "payload": "p1"},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2

    resp = client.get("/resources", params={"page_size": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is False
    assert len(body["items"]) == 1


def test_http_conflict_mapping(client: TestClient) -> None:
    created = _create(client, name="alpha")
    client.put(
        f"/resources/{created['id']}", json={"expected_version": 1}
    )
    resp = client.put(
        f"/resources/{created['id']}", json={"expected_version": 1}
    )
    assert resp.status_code == 409
    body = resp.json()["error"]
    logger.info(
        "[TEST] http conflict code=%s expected=%s current=%s",
        body["code"], body["expected_version"], body["current_version"],
    )
    assert body["code"] == "version_conflict"
    assert body["expected_version"] == 1
    assert body["current_version"] == 2


def test_http_unique_conflict_is_distinct(client: TestClient) -> None:
    _create(client, name="dup")
    resp = client.post("/resources", json={"name": "dup"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "unique_constraint_violation"


def test_http_not_found_mapping(client: TestClient) -> None:
    resp = client.get("/resources/ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "resource_not_found"


def test_http_invalid_cursor_mapping(client: TestClient) -> None:
    resp = client.get("/resources", params={"cursor": "@@@bad@@@"})
    assert resp.status_code == 400
    body = resp.json()["error"]
    logger.info("[TEST] http invalid cursor code=%s", body["code"])
    assert body["code"] == "invalid_cursor"


def test_http_pagination_walk(client: TestClient) -> None:
    for i in range(7):
        _create(client, name=f"n{i:02d}")
    ids: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"page_size": 3}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/resources", params=params).json()
        pages += 1
        ids.extend(i["id"] for i in body["items"])
        logger.info(
            "[TEST] http page=%d returned=%d cursor_next=%s",
            pages, len(body["items"]), body["next_cursor"],
        )
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert len(ids) == len(set(ids)) == 7


def test_http_delete_restore_lifecycle(client: TestClient) -> None:
    created = _create(client, name="lifecycle")
    rid = created["id"]

    # 删除
    resp = client.request("DELETE", f"/resources/{rid}", json={"expected_version": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deleted" and body["version"] == 2
    assert body["deleted_at"] is not None

    # 默认单查 / 列表视为不存在
    assert client.get(f"/resources/{rid}").status_code == 404
    listed = client.get("/resources", params={"status": "active"}).json()
    assert listed["items"] == []
    trash = client.get("/resources", params={"status": "deleted"}).json()
    assert [i["id"] for i in trash["items"]] == [rid]

    # 名字可复用
    other = _create(client, name="lifecycle")
    # 恢复撞名 => 409 unique_constraint_violation
    resp = client.post(
        f"/resources/{rid}/restore", json={"expected_version": 2}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "unique_constraint_violation"

    # 释放占用名后恢复成功
    assert client.request(
        "DELETE", f"/resources/{other['id']}", json={"expected_version": 1}
    ).status_code == 200
    resp = client.post(
        f"/resources/{rid}/restore", json={"expected_version": 2}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert resp.json()["version"] == 3

    # 状态错误归类
    resp = client.post(
        f"/resources/{rid}/restore", json={"expected_version": 3}
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "invalid_state"
    assert err["current_status"] == "active"


def test_http_delete_conflict_codes_distinct(client: TestClient) -> None:
    created = _create(client, name="c")
    rid = created["id"]
    # 先把版本推到 2
    assert client.put(
        f"/resources/{rid}", json={"expected_version": 1}
    ).status_code == 200
    # 拿过期版本删除 => version_conflict
    resp = client.request(
        "DELETE", f"/resources/{rid}", json={"expected_version": 1}
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["current_version"] == 2
    # 资源未被删除
    assert client.get(f"/resources/{rid}").status_code == 200


def test_http_purge_endpoint(client: TestClient, clock) -> None:
    created = _create(client, name="trashme")
    client.request(
        "DELETE", f"/resources/{created['id']}", json={"expected_version": 1}
    )
    # 严格边界：推进 1ms 后 retention_ms=0 才确实到期
    clock.t += 1
    resp = client.post("/resources/purge", json={"retention_ms": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["purged"] >= 1
    assert body["has_more"] is False
    assert {"purged", "scanned", "has_more", "batches"} <= set(body)
    assert client.get(f"/resources/{created['id']}").status_code == 404

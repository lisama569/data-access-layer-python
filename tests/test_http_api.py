"""HTTP 入口行为测试：错误在 API 入口下同样得到一致归类。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.connection import Database
from app.http_api import create_app

logger = logging.getLogger("app.dal")


@pytest.fixture
def client(db: Database) -> TestClient:
    return TestClient(create_app(db))


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


def test_http_lifecycle_delete_restore(client: TestClient) -> None:
    r = _create(client, name="lifecycle", payload="p0")

    # 删除
    resp = client.post(
        f"/resources/{r['id']}/delete", json={"expected_version": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deleted" and body["version"] == 2
    assert body["deleted_at"] is not None

    # 默认单查 404，默认列表看不到
    assert client.get(f"/resources/{r['id']}").status_code == 404
    listed = client.get("/resources", params={"page_size": 10}).json()
    assert listed["items"] == []

    # 回收站视图可见
    trash = client.get(
        "/resources", params={"page_size": 10, "status": "deleted"}
    ).json()
    assert [i["id"] for i in trash["items"]] == [r["id"]]

    # 旧版本删除 => 409 version_conflict
    resp = client.post(
        f"/resources/{r['id']}/delete", json={"expected_version": 1}
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["current_version"] == 2

    # 当前版本重复删 => 409 resource_state_conflict
    resp = client.post(
        f"/resources/{r['id']}/delete", json={"expected_version": 2}
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "resource_state_conflict"
    assert err["current_state"] == "deleted"

    # 恢复
    resp = client.post(
        f"/resources/{r['id']}/restore", json={"expected_version": 2}
    )
    assert resp.status_code == 200
    restored = resp.json()
    assert restored["status"] == "active" and restored["version"] == 3
    assert restored["deleted_at"] is None
    assert client.get(f"/resources/{r['id']}").status_code == 200


def test_http_delete_missing_is_404(client: TestClient) -> None:
    resp = client.post("/resources/ghost/delete", json={"expected_version": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "resource_not_found"


def test_http_invalid_status_filter_is_400(client: TestClient) -> None:
    resp = client.get("/resources", params={"status": "archived"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"


def test_http_purge_endpoint(client: TestClient) -> None:
    r = _create(client, name="trashme")
    assert client.post(
        f"/resources/{r['id']}/delete", json={"expected_version": 1}
    ).status_code == 200
    # retention_ms=0：已删除记录立即超期
    resp = client.post(
        "/maintenance/purge-deleted",
        json={"retention_ms": 0, "batch_size": 50},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["purged_count"] == 1
    assert body["batch_count"] == 1
    assert body["has_more"] is False
    assert "cutoff_ms" in body and "retention_ms" in body
    # 物理清除后默认单查 404，回收站视图为空
    assert client.get(f"/resources/{r['id']}").status_code == 404
    assert client.get(
        "/resources", params={"status": "deleted"}
    ).json()["items"] == []


def test_http_purge_invalid_body_is_400(client: TestClient) -> None:
    resp = client.post(
        "/maintenance/purge-deleted", json={"retention_ms": -1}
    )
    assert resp.status_code == 422  # pydantic 入参校验

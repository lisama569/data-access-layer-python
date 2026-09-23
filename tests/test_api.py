"""HTTP 入口下错误归类与语义一致性测试。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

logger = logging.getLogger("dal.tests")


@pytest.fixture()
def client(db_path: str):
    from app import services as services_mod

    services_mod._initialized_paths.clear()
    with TestClient(create_app()) as c:  # 触发 startup 建表
        yield c


def _create(client: TestClient, name: str = "r", status: str = "active") -> dict:
    resp = client.post("/resources", json={"name": name, "status": status})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_get_list_http(client: TestClient) -> None:
    created = _create(client, "web", "active")
    assert created["version"] == 1

    got = client.get(f"/resources/{created['id']}")
    assert got.status_code == 200
    assert got.json()["version"] == 1

    page = client.get("/resources?limit=10")
    assert page.status_code == 200
    body = page.json()
    assert body["items"][0]["id"] == created["id"]
    assert "next_cursor" in body and body["has_more"] is False
    logger.info("HTTP CREATE/GET version=%s cursor=<END>", created["version"])


def test_http_conflict_status_and_reason(client: TestClient) -> None:
    created = _create(client, "conflict", "active")
    ok = client.put(
        f"/resources/{created['id']}",
        json={"name": "v2", "status": "active", "expected_version": 1},
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2

    stale = client.put(
        f"/resources/{created['id']}",
        json={"name": "lost", "status": "active", "expected_version": 1},
    )
    assert stale.status_code == 409
    payload = stale.json()
    assert payload["reason"] == "version_conflict"
    assert "actual 2" in payload["detail"]
    logger.info(
        "HTTP CONFLICT result=REJECTED reason=%s expected=1 actual=2",
        payload["reason"],
    )

    # 未被覆盖：已提交的名称仍为 v2。
    final = client.get(f"/resources/{created['id']}").json()
    assert final["name"] == "v2" and final["version"] == 2


def test_http_not_found(client: TestClient) -> None:
    resp = client.get("/resources/missing")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "not_found"
    logger.info("HTTP GET missing -> 404 reason=not_found")


def test_http_invalid_cursor(client: TestClient) -> None:
    _create(client)
    resp = client.get("/resources?cursor=%%%garbage%%%")
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_cursor"
    logger.info("HTTP LIST invalid cursor -> 400 reason=invalid_cursor")


def test_http_validation_error_shape(client: TestClient) -> None:
    # 缺少 expected_version：Pydantic 422，不进入数据访问层。
    created = _create(client)
    resp = client.put(
        f"/resources/{created['id']}",
        json={"name": "x", "status": "active"},
    )
    assert resp.status_code == 422

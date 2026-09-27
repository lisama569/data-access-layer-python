"""旧结构数据库（无生命周期列、name 表级 UNIQUE）的在线迁移。"""
from __future__ import annotations

import sqlite3

from app.connection import Database
from app.models import ResourceCreate, ResourceQuery
from app.service import ResourceService


def test_legacy_schema_migrates_and_keeps_data(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE resources ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
        "payload TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    old.execute(
        "INSERT INTO resources (id,name,payload,version,created_at,updated_at) "
        "VALUES ('a','alpha','p0',3,10,20)"
    )
    old.commit()
    old.close()

    db = Database(path)
    db.initialize()  # 迁移

    service = ResourceService(db)
    r = service.get_resource("a")
    assert r.version == 3 and r.status == "active" and r.deleted_at is None
    assert service.list_resources(ResourceQuery(page_size=10)).items[0].id == "a"

    # 迁移后生命周期能力可用：名字能被释放/复用
    from app.models import ResourceStateChange

    service.delete_resource(ResourceStateChange(id="a", expected_version=3))
    created = service.create_resource(ResourceCreate(name="alpha"))
    assert created.id != "a"

    # 再次 initialize 幂等，不报错不丢数据
    db.initialize()
    assert service.list_resources(
        ResourceQuery(status="all", page_size=10)
    ).items.__len__() == 2

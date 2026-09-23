"""pytest 公共夹具：每个测试使用独立的临时文件数据库。"""
from __future__ import annotations

import logging

import pytest

from app.connection import Database
from app.service import ResourceService


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    database.initialize()
    return database


@pytest.fixture
def service(db: Database) -> ResourceService:
    return ResourceService(db)


@pytest.fixture(autouse=True)
def _enable_dal_logs(caplog) -> None:
    # 让版本号 / 事务结果 / 游标位置在单测日志中可见
    caplog.set_level(logging.INFO, logger="app.dal")

"""pytest 夹具与日志：每个测试使用独立临时数据库。

单测日志统一通过 logger "dal.tests" 打印三类关键信息：
- 版本号（version / expected / actual）
- 事务结果（COMMITTED / ROLLED_BACK / ABORTED）
- 游标位置（next_cursor 是否为空）
使用 ``uv run pytest -s`` 可直接看到这些输出。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest

logger = logging.getLogger("dal.tests")


@pytest.fixture(scope="session", autouse=True)
def _configure_logging() -> Iterator[None]:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    dal_logger = logging.getLogger("dal")
    dal_logger.setLevel(logging.DEBUG)
    dal_logger.addHandler(handler)
    dal_logger.propagate = False
    yield
    dal_logger.removeHandler(handler)


@pytest.fixture()
def db_path(tmp_path) -> Iterator[str]:
    path = str(tmp_path / "test.db")
    old = os.environ.get("DAL_DB_PATH")
    os.environ["DAL_DB_PATH"] = path
    # 服务层按路径缓存建表状态，新临时库需清除缓存。
    from app import services as services_mod

    services_mod._initialized_paths.clear()
    yield path
    if old is None:
        os.environ.pop("DAL_DB_PATH", None)
    else:
        os.environ["DAL_DB_PATH"] = old


@pytest.fixture()
def service(db_path: str):
    from app.services import ResourceService

    return ResourceService(db_path)

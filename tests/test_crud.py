"""基础增改查测试。"""

from __future__ import annotations

import logging

import pytest

from app.errors import NotFoundError

logger = logging.getLogger("dal.tests")


def test_create_and_get(service) -> None:
    resource = service.create_resource("alpha", "active")
    logger.info("CREATE id=%s version=%s", resource.id, resource.version)
    assert resource.version == 1

    fetched = service.get_resource(resource.id)
    assert fetched.name == "alpha"
    assert fetched.status == "active"
    assert fetched.version == 1
    logger.info("GET id=%s version=%s", fetched.id, fetched.version)


def test_update_increments_version(service) -> None:
    resource = service.create_resource("beta", "pending")
    updated = service.update_resource(resource.id, "beta-2", "active", 1)
    logger.info("UPDATE result=COMMITTED id=%s version=%s", updated.id, updated.version)
    assert updated.version == 2
    assert updated.name == "beta-2"

    updated2 = service.update_resource(resource.id, "beta-3", "archived", 2)
    assert updated2.version == 3
    logger.info("UPDATE result=COMMITTED id=%s version=%s", updated2.id, updated2.version)


def test_get_missing_raises_not_found(service) -> None:
    with pytest.raises(NotFoundError):
        service.get_resource("does-not-exist")
    logger.info("GET missing -> reason=not_found")


def test_create_many_commits_atomically(service) -> None:
    created = service.create_many([("a", "s"), ("b", "s"), ("c", "s")])
    versions = [r.version for r in created]
    logger.info("CREATE_MANY result=COMMITTED versions=%s", versions)
    assert versions == [1, 1, 1]
    page = service.list_resources(limit=50)
    assert len(page.items) == 3

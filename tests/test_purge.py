"""回收站保留期清理。

- 只清 deleted_at + retention <= now 的资源，保留期内/已恢复一律不动；
- 分批推进：单批只删 batch_size 条，批间各自短事务（不长期持锁）；
- 失败重试结果一致：已提交的批不重复清理，未到期不会被误清；
- 清理结果用 PurgeResult 解释（purged/scanned/has_more/batches）；
- 底层存储细节不直接透出。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import InvalidInputError, ResourceNotFoundError
from app.models import ResourceCreate, ResourceStateChange
from app.service import ResourceService

logger = logging.getLogger("app.dal")


class FakeClock:
    def __init__(self, start: int = 1_000) -> None:
        self.t = start

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tservice(db, clock: FakeClock) -> ResourceService:
    return ResourceService(db, default_retention_ms=1_000, clock=clock)


def _create_and_delete(service: ResourceService, name: str, clock: FakeClock):
    r = service.create_resource(ResourceCreate(name=name))
    clock.t += 10
    d = service.delete_resource(
        ResourceStateChange(id=r.id, expected_version=1)
    )
    return r.id, d.deleted_at


def test_retention_keeps_recent_deletes(tservice, clock) -> None:
    rid, deleted_at = _create_and_delete(tservice, "fresh", clock)
    # 距离删除还没到保留期：一条都不能清
    result = tservice.purge_deleted(now_ms=deleted_at + 999)
    assert result.purged == 0 and result.has_more is False
    assert (
        tservice.get_resource(rid, include_deleted=True).status == "deleted"
    )


def test_retention_boundary_exactly_at_limit_is_kept(tservice, clock) -> None:
    """时间点刚好卡在保留期上（deleted_at + retention == now）也不能清。

    只有确实**超过**保留时长（>）才进入清理范围，实际可恢复时间不会比
    配置的保留时长更短。
    """
    rid, deleted_at = _create_and_delete(tservice, "edge", clock)
    # 恰好满保留期：必须保留
    at_limit = tservice.purge_deleted(now_ms=deleted_at + 1_000)
    assert at_limit.purged == 0 and at_limit.has_more is False
    assert (
        tservice.get_resource(rid, include_deleted=True).id == rid
    )
    # 重试（同一时间点）仍然不能误伤
    retry = tservice.purge_deleted(now_ms=deleted_at + 1_000)
    assert retry.purged == 0 and retry.has_more is False
    assert (
        tservice.get_resource(rid, include_deleted=True).id == rid
    )
    # 真正超过保留期 1ms：才允许清掉
    overdue = tservice.purge_deleted(now_ms=deleted_at + 1_001)
    assert overdue.purged == 1 and overdue.has_more is False
    with pytest.raises(ResourceNotFoundError):
        tservice.get_resource(rid, include_deleted=True)


def test_purge_removes_only_expired(tservice, clock) -> None:
    old_id, old_at = _create_and_delete(tservice, "old", clock)
    new_id, new_at = _create_and_delete(tservice, "new", clock)
    # old 已确实超过保留期；new 连“恰好满保留期”都还没到
    now = old_at + 1_001
    assert now < new_at + 1_000
    result = tservice.purge_deleted(now_ms=now)
    assert result.purged == 1 and result.scanned == 1
    with pytest.raises(ResourceNotFoundError):
        tservice.get_resource(old_id, include_deleted=True)
    assert (
        tservice.get_resource(new_id, include_deleted=True).id == new_id
    )


def test_zero_retention_purges_only_strictly_before_now(tservice, clock) -> None:
    """retention_ms=0：同样遵守严格边界，deleted_at < now 才清。"""
    rid, deleted_at = _create_and_delete(tservice, "now", clock)
    # 与删除同一毫秒调用：边界相等，不能清
    at_same_ms = tservice.purge_deleted(retention_ms=0, now_ms=deleted_at)
    assert at_same_ms.purged == 0
    assert (
        tservice.get_resource(rid, include_deleted=True).id == rid
    )
    # 时间向前走 1ms 后：确实超过（0 时长）-> 清除
    result = tservice.purge_deleted(retention_ms=0, now_ms=deleted_at + 1)
    assert result.purged == 1 and result.has_more is False
    with pytest.raises(ResourceNotFoundError):
        tservice.get_resource(rid, include_deleted=True)


def test_active_and_restored_never_purged(tservice, clock) -> None:
    active = tservice.create_resource(ResourceCreate(name="active"))
    rid, _ = _create_and_delete(tservice, "will-restore", clock)
    restored = tservice.restore_resource(
        ResourceStateChange(id=rid, expected_version=2)
    )
    assert restored.status == "active"
    clock.t += 100_000
    result = tservice.purge_deleted()
    assert result.purged == 0
    # 两个 active 资源都还在
    assert tservice.get_resource(active.id).id == active.id
    assert tservice.get_resource(rid).id == rid


def test_purge_batching_advances_without_long_lock(tservice, clock) -> None:
    ids = [
        _create_and_delete(tservice, f"d{i:02d}", clock)[0] for i in range(7)
    ]
    clock.t += 100_000
    # 每批 3 条，最多 2 批 => 清 6 条，明确报告还有积压
    first = tservice.purge_deleted(batch_size=3, max_batches=2)
    assert first.purged == 6 and first.batches == 2 and first.has_more is True
    survivors = [
        tservice.get_resource(i, include_deleted=True).id
        for i in ids
        if _exists(tservice, i)
    ]
    assert len(survivors) == 1
    # 再次调用清掉最后一批，has_more 归 False
    second = tservice.purge_deleted(batch_size=3)
    assert second.purged == 1 and second.has_more is False
    assert all(not _exists(tservice, i) for i in ids)


def _exists(service: ResourceService, rid: str) -> bool:
    from app.errors import ResourceNotFoundError as NF

    try:
        service.get_resource(rid, include_deleted=True)
        return True
    except NF:
        return False


def test_last_batch_that_empties_backlog_reports_has_more_false(
    tservice, clock
) -> None:
    """积压条数恰好是批大小的整数倍：最后一批清空后必须明确结束。

    旧实现用 ``purged == batch_size`` 推断“还有下一批”，最后一批恰好
    清空时仍报 has_more=True，误导调用方继续跑。现在 has_more 反映
    清理后的真实剩余情况。
    """
    ids = [
        _create_and_delete(tservice, f"e{i:02d}", clock)[0] for i in range(6)
    ]
    clock.t += 100_000
    result = tservice.purge_deleted(batch_size=3)
    assert result.purged == 6 and result.batches == 2
    assert result.has_more is False  # 最后一批恰好清空：不得再报有积压
    assert all(not _exists(tservice, i) for i in ids)

    # 单批恰好清空（积压 == batch_size）同样必须报结束
    ids2 = [
        _create_and_delete(tservice, f"s{i:02d}", clock)[0] for i in range(3)
    ]
    clock.t += 100_000
    one_batch = tservice.purge_deleted(batch_size=3)
    assert one_batch.purged == 3 and one_batch.batches == 1
    assert one_batch.has_more is False
    assert all(not _exists(tservice, i) for i in ids2)

    # 清空后立刻重试：无到期数据，幂等且依旧明确结束
    again = tservice.purge_deleted(batch_size=3)
    assert again.purged == 0 and again.has_more is False


def test_retry_after_partial_failure_is_consistent(
    tservice, clock, monkeypatch
) -> None:
    ids = [
        _create_and_delete(tservice, f"r{i:02d}", clock)[0] for i in range(6)
    ]
    clock.t += 100_000

    calls = {"n": 0}
    real = type(tservice).purge_deleted

    # 让“外部调用方”在第一批提交成功后、第二批前遭遇失败：
    # 通过让 repository 在第 2 批查询时抛异常来模拟中途失败。
    from app import repository as repo_mod

    orig = repo_mod.ResourceRepository.purge_expired

    def flaky(self, *, now_ms, retention_ms, batch_size):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated storage hiccup")
        return orig(self, now_ms=now_ms, retention_ms=retention_ms, batch_size=batch_size)

    monkeypatch.setattr(repo_mod.ResourceRepository, "purge_expired", flaky)

    with pytest.raises(Exception):
        tservice.purge_deleted(batch_size=2)

    # 第一批（2 条）已提交；第二批整体回滚
    still = [i for i in ids if _exists(tservice, i)]
    assert len(still) == 4
    assert len(ids) - len(still) == 2

    # 恢复实现后重试：剩余 4 条被清，总计恰好 6 条，无重复清理/状态错乱
    monkeypatch.undo()
    retry = tservice.purge_deleted(batch_size=2)
    assert retry.purged == 4 and retry.has_more is False
    assert all(not _exists(tservice, i) for i in ids)
    # 再重试一次：已无到期数据，幂等
    again = tservice.purge_deleted(batch_size=2)
    assert again.purged == 0


def test_retry_keeps_boundary_rows_after_partial_failure(
    tservice, clock, monkeypatch
) -> None:
    """中途失败后重试：已提交批次不重复清理，卡在保留期边界的行绝不被误伤。"""
    from app import repository as repo_mod

    # 三条早已到期；一条“刚好卡在保留期上”
    expired = [
        _create_and_delete(tservice, f"o{i:02d}", clock)[0] for i in range(3)
    ]
    edge_id, edge_at = _create_and_delete(tservice, "edge", clock)
    purge_now = edge_at + 1_000  # edge 恰好满保留期：不得清

    calls = {"n": 0}
    orig = repo_mod.ResourceRepository.purge_expired

    def flaky(self, *, now_ms, retention_ms, batch_size):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated storage hiccup")
        return orig(self, now_ms=now_ms, retention_ms=retention_ms, batch_size=batch_size)

    monkeypatch.setattr(repo_mod.ResourceRepository, "purge_expired", flaky)
    with pytest.raises(Exception):
        tservice.purge_deleted(batch_size=2, now_ms=purge_now)

    monkeypatch.undo()
    # 同一时间点重试：到期的清掉，边界行原样保留
    retry = tservice.purge_deleted(batch_size=2, now_ms=purge_now)
    assert retry.has_more is False
    assert all(not _exists(tservice, i) for i in expired)
    assert _exists(tservice, edge_id)
    edge = tservice.get_resource(edge_id, include_deleted=True)
    assert edge.status == "deleted" and edge.deleted_at == edge_at

    # 真正超过保留期后，边界行才被清
    final = tservice.purge_deleted(now_ms=purge_now + 1)
    assert final.purged == 1 and final.has_more is False
    assert not _exists(tservice, edge_id)


def test_invalid_purge_arguments_rejected(tservice) -> None:
    with pytest.raises(InvalidInputError):
        tservice.purge_deleted(retention_ms=-1)
    with pytest.raises(InvalidInputError):
        tservice.purge_deleted(batch_size=0)
    with pytest.raises(InvalidInputError):
        tservice.purge_deleted(max_batches=0)


def test_purge_result_is_explainable(tservice, clock) -> None:
    _create_and_delete(tservice, "x", clock)
    clock.t += 100_000
    result = tservice.purge_deleted()
    assert result.batches >= 1
    assert isinstance(result.purged, int)
    assert isinstance(result.scanned, int)
    assert isinstance(result.has_more, bool)

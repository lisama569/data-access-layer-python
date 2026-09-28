"""回收站保留期清理。

- 只清严格超过保留期的行：deleted_at + retention_ms < now；
  刚好卡在边界（==）仍在保留期内，保留期内/已恢复一律不动；
- 分批推进：单批只删 batch_size 条，批间各自短事务（不长期持锁）；
- 失败重试结果一致：已提交的批不重复清理，未到期不会被误清；
- 清理结果用 PurgeResult 解释（purged/scanned/has_more/batches）；
- 底层存储细节不直接透出。
"""
from __future__ import annotations

import logging

import pytest

from app.errors import InvalidInputError, ResourceNotFoundError, TransactionError
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


def test_purge_removes_only_expired(tservice, clock) -> None:
    old_id, old_at = _create_and_delete(tservice, "old", clock)
    new_id, new_at = _create_and_delete(tservice, "new", clock)
    # 严格超过保留期（deleted_at + 1000 < now）才到期：取 old_at + 1001
    # （old 已到期），且 new 还没到期（now < new_at + 1000）
    now = old_at + 1_001
    assert now < new_at + 1_000
    result = tservice.purge_deleted(now_ms=now)
    assert result.purged == 1 and result.scanned == 1
    with pytest.raises(ResourceNotFoundError):
        tservice.get_resource(old_id, include_deleted=True)
    assert (
        tservice.get_resource(new_id, include_deleted=True).id == new_id
    )


def test_retention_boundary_exactly_at_limit_is_kept(tservice, clock) -> None:
    """刚好卡在保留期边界（deleted_at + retention == now）不能清。

    边界语义为严格超过才到期：边界时刻记录仍可恢复，必须留在回收站；
    再过去 1ms 才进入清理范围。
    """
    rid, deleted_at = _create_and_delete(tservice, "edge", clock)
    boundary = deleted_at + 1_000
    # 边界点：一条都不能清，has_more 也必须明确为 False
    at_boundary = tservice.purge_deleted(now_ms=boundary)
    assert at_boundary.purged == 0
    assert at_boundary.scanned == 0
    assert at_boundary.has_more is False
    assert (
        tservice.get_resource(rid, include_deleted=True).status == "deleted"
    )
    # 边界前 1ms 同样不能清
    before = tservice.purge_deleted(now_ms=boundary - 1)
    assert before.purged == 0 and before.has_more is False
    assert (
        tservice.get_resource(rid, include_deleted=True).status == "deleted"
    )
    # 严格超过边界 1ms：进入清理范围
    after = tservice.purge_deleted(now_ms=boundary + 1)
    assert after.purged == 1 and after.has_more is False
    with pytest.raises(ResourceNotFoundError):
        tservice.get_resource(rid, include_deleted=True)


def test_zero_retention_requires_strictly_past_ms(tservice, clock) -> None:
    """retention_ms=0 也遵守严格边界：deleted_at 必须严格早于清理时刻。"""
    rid, deleted_at = _create_and_delete(tservice, "now", clock)
    # 与删除同一毫秒：尚未“超过”保留时长，不能清
    same_ms = tservice.purge_deleted(retention_ms=0, now_ms=deleted_at)
    assert same_ms.purged == 0 and same_ms.has_more is False
    assert (
        tservice.get_resource(rid, include_deleted=True).status == "deleted"
    )
    # 过 1ms 后：确实超过 0 保留时长，立即清除
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

    # 底层 RuntimeError 不得原样透出：统一包装为数据访问层事务错误
    with pytest.raises(TransactionError) as exc_info:
        tservice.purge_deleted(batch_size=2)
    assert not isinstance(exc_info.value, RuntimeError)
    assert exc_info.value.code == "transaction_failed"

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


def test_last_batch_exactly_drains_reports_finished(tservice, clock) -> None:
    """到期总数恰好是批大小整数倍：最后一批清空后必须明确结束。

    旧实现用 ``本批清除数 == batch_size`` 推断还有下一批，最后一批恰好清空时
    仍误报 has_more=True。这里要求 has_more 反映清理后的真实剩余。
    """
    ids = [
        _create_and_delete(tservice, f"e{i:02d}", clock)[0] for i in range(6)
    ]
    clock.t += 100_000
    # 6 条 / 每批 3 条：第二批恰好清空，返回必须 has_more=False
    result = tservice.purge_deleted(batch_size=3)
    assert result.purged == 6
    assert result.batches == 2
    assert result.has_more is False
    assert all(not _exists(tservice, i) for i in ids)
    # 已清空后再来一次：空跑一批，仍然明确结束
    again = tservice.purge_deleted(batch_size=3)
    assert again.purged == 0 and again.scanned == 0
    assert again.batches == 1 and again.has_more is False


def test_last_batch_undivided_drains_reports_finished(tservice, clock) -> None:
    """到期总数不是批大小整数倍：最后一批不足额清空同样明确结束。"""
    ids = [
        _create_and_delete(tservice, f"u{i:02d}", clock)[0] for i in range(7)
    ]
    clock.t += 100_000
    result = tservice.purge_deleted(batch_size=3)
    assert result.purged == 7 and result.has_more is False
    assert all(not _exists(tservice, i) for i in ids)


def test_max_batches_leaves_true_remaining(tservice, clock) -> None:
    """max_batches 截断时仍如实报告剩余：未跑完且确实还有到期行。"""
    ids = [
        _create_and_delete(tservice, f"m{i:02d}", clock)[0] for i in range(7)
    ]
    clock.t += 100_000
    first = tservice.purge_deleted(batch_size=3, max_batches=2)
    assert first.purged == 6 and first.batches == 2
    assert first.has_more is True  # 还剩 1 条到期行
    assert len([i for i in ids if _exists(tservice, i)]) == 1


def test_retry_after_failure_keeps_within_retention(tservice, clock, monkeypatch) -> None:
    """清理中途失败后重试：保留期内（含边界）记录绝不被误伤。

    7 条已到期（保证第 2 批真实存在并在其中途失败）、2 条仍在保留期
    （其中一条恰好卡在边界 deleted_at + R == now）。第一批提交后、第二批前
    失败；重试只能清剩余的到期行，边界记录与保留期内记录必须原样留在回收站，
    且重试后 has_more 如实为 False。
    """
    expired_ids = [
        _create_and_delete(tservice, f"old{i:02d}", clock)[0] for i in range(7)
    ]
    # 保留期内的两条（都比到期行删除得晚）：
    # edge 恰好卡在边界 deleted_at + retention == now（不可清）；
    # inside 删除更晚，明显还在保留期内。
    edge_id, edge_at = _create_and_delete(tservice, "edge", clock)
    inside_id, inside_at = _create_and_delete(tservice, "inside", clock)
    assert edge_at < inside_at
    # 7 条旧行删除于 [edge_at-70, edge_at-10]，均严格早于 edge_at；
    # 取 purge_now = edge_at + 1000 时，旧行全部严格到期，而 edge 恰好
    # deleted_at + retention == now 卡在边界（必须保留）。
    purge_now = edge_at + 1_000
    assert purge_now < inside_at + 1_000

    from app import repository as repo_mod

    orig = repo_mod.ResourceRepository.purge_expired
    calls = {"n": 0}

    def flaky(self, *, now_ms, retention_ms, batch_size):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated storage hiccup")
        return orig(
            self, now_ms=now_ms, retention_ms=retention_ms, batch_size=batch_size
        )

    monkeypatch.setattr(repo_mod.ResourceRepository, "purge_expired", flaky)
    # 底层异常不外泄：包装为数据访问层的事务错误，第二批整体回滚
    with pytest.raises(TransactionError) as exc_info:
        tservice.purge_deleted(batch_size=3, now_ms=purge_now)
    assert exc_info.value.code == "transaction_failed"

    # 第一批 3 条到期行已提交；第二批在执行前失败并整体回滚，其余到期行、
    # 边界行、保留期内行都还在
    remaining = [i for i in expired_ids if _exists(tservice, i)]
    assert len(remaining) == 4
    assert _exists(tservice, edge_id)
    assert _exists(tservice, inside_id)

    # 恢复实现后重试：剩余 4 条到期行被清，边界/保留期记录不动
    monkeypatch.undo()
    retry = tservice.purge_deleted(batch_size=3, now_ms=purge_now)
    assert retry.purged == 4 and retry.has_more is False
    assert all(not _exists(tservice, i) for i in expired_ids)
    edge = tservice.get_resource(edge_id, include_deleted=True)
    inside = tservice.get_resource(inside_id, include_deleted=True)
    assert edge.status == "deleted" and edge.deleted_at == edge_at
    assert inside.status == "deleted" and inside.deleted_at == inside_at
    # 再重试幂等：没有到期数据，也不碰保留期内记录
    again = tservice.purge_deleted(batch_size=3, now_ms=purge_now)
    assert again.purged == 0 and again.has_more is False
    assert _exists(tservice, edge_id) and _exists(tservice, inside_id)
    # 时间再越过边界后，edge 才进入清理范围；inside 仍在保留期
    later = tservice.purge_deleted(batch_size=3, now_ms=edge_at + 1_001)
    assert later.purged == 1
    assert not _exists(tservice, edge_id)
    assert _exists(tservice, inside_id)


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

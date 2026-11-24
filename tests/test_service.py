"""TransferService 核心逻辑测试：不变式、失败回退、崩溃恢复。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.service as service_mod
from app.cgroups import read_cpu_max
from app.journal import STATE_IN_PROGRESS, STATE_REGISTERED, Journal
from app.service import (
    RecoveryPendingError,
    TransferError,
    TransferService,
    WriteFailureError,
)

A, B = "groupA", "groupB"
PERIOD = 100000
MIN_Q = 20000
INIT_A, INIT_B = 100000, 50000
TOTAL = INIT_A + INIT_B


def write_cpu_max_file(path: Path, quota, period=PERIOD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{quota} {period}\n")


@pytest.fixture
def env(tmp_path):
    cg = tmp_path / "cgroup"
    state = tmp_path / "state"
    write_cpu_max_file(cg / A / "cpu.max", INIT_A)
    write_cpu_max_file(cg / B / "cpu.max", INIT_B)
    svc = TransferService({A: cg / A / "cpu.max", B: cg / B / "cpu.max"}, state, MIN_Q)
    yield svc, cg, state
    svc.close()


def quota(svc, g) -> int:
    return read_cpu_max(svc.group_paths[g]).quota_value


def make_service(cg, state, crash_hook=None) -> TransferService:
    return TransferService(
        {A: cg / A / "cpu.max", B: cg / B / "cpu.max"}, state, MIN_Q, crash_hook
    )


# ---------------------------------------------------------------- 基本查询/恢复


def test_recovery_without_journal(env):
    svc, _, _ = env
    res = svc.recover()
    assert svc.recovered is True
    assert res["result"] == "nothing_pending"
    assert svc.last_result is None  # 无未决转移时不覆盖历史结果


def test_transfer_rejected_before_recovery(env):
    svc, _, _ = env
    with pytest.raises(RecoveryPendingError):
        svc.transfer(B, 1000)


# ---------------------------------------------------------------- 正常转移


def test_successful_transfer(env):
    svc, _, _ = env
    svc.recover()
    res = svc.transfer(B, 20000)
    assert res["result"] == "success"
    assert res["receiver"] == A
    assert quota(svc, A) == 120000
    assert quota(svc, B) == 30000
    assert quota(svc, A) + quota(svc, B) == TOTAL  # 总额不变
    assert svc.journal.read() is None  # 日志已清理
    assert svc.last_result["result"] == "success"


def test_sum_never_exceeds_total_during_transfer(env, monkeypatch):
    """记录每次写文件后的磁盘总额，验证转移全程不超过转移前总额。"""
    svc, _, _ = env
    svc.recover()
    orig = service_mod.write_cpu_max
    sums = []
    values = []

    def spy(path, cm):
        orig(path, cm)
        values.append(int(cm.quota))
        sums.append(sum(read_cpu_max(p).quota_value for p in svc.group_paths.values()))

    monkeypatch.setattr(service_mod, "write_cpu_max", spy)
    svc.transfer(B, 20000)
    assert values == [30000, 120000]  # 先降让出方，后升接收方
    assert all(s <= TOTAL for s in sums)


def test_floor_boundary_allowed(env):
    svc, _, _ = env
    svc.recover()
    svc.transfer(B, INIT_B - MIN_Q)  # 让出方恰好降到下限
    assert quota(svc, B) == MIN_Q


def test_floor_violation_rejected(env):
    svc, _, _ = env
    svc.recover()
    with pytest.raises(TransferError):
        svc.transfer(B, INIT_B - MIN_Q + 1)
    assert quota(svc, A) == INIT_A and quota(svc, B) == INIT_B  # 未动文件


def test_invalid_requests_rejected(env):
    svc, _, _ = env
    svc.recover()
    with pytest.raises(TransferError):
        svc.transfer("groupC", 1000)
    for bad in (0, -5):
        with pytest.raises(TransferError):
            svc.transfer(B, bad)
    assert quota(svc, A) == INIT_A and quota(svc, B) == INIT_B


def test_period_mismatch_rejected(env):
    svc, cg, _ = env
    write_cpu_max_file(cg / B / "cpu.max", INIT_B, period=50000)
    svc.recover()
    with pytest.raises(TransferError, match="period"):
        svc.transfer(B, 1000)


def test_max_quota_rejected(env):
    svc, cg, _ = env
    write_cpu_max_file(cg / B / "cpu.max", "max")
    svc.recover()
    with pytest.raises(TransferError, match="max"):
        svc.transfer(B, 1000)


# ---------------------------------------------------------------- 写失败回退


def test_write_failure_rolls_back(env, monkeypatch):
    svc, cg, _ = env
    svc.recover()
    orig = service_mod.write_cpu_max
    receiver_path = svc.group_paths[A]

    def flaky(path, cm):
        if Path(path) == receiver_path:
            raise OSError("模拟接收方写失败")
        return orig(path, cm)

    monkeypatch.setattr(service_mod, "write_cpu_max", flaky)
    with pytest.raises(WriteFailureError):
        svc.transfer(B, 20000)
    # 已恢复转移前配额
    assert quota(svc, A) == INIT_A and quota(svc, B) == INIT_B
    assert svc.journal.read() is None
    assert svc.last_result["result"] == "failed"
    assert svc.last_result["rolled_back"] is True
    # 回退后可继续正常转移
    monkeypatch.setattr(service_mod, "write_cpu_max", orig)
    svc.transfer(B, 10000)
    assert quota(svc, A) == 110000


# ---------------------------------------------------------------- 崩溃与重启恢复


class SimulatedCrash(BaseException):
    """模拟进程崩溃：必须绕过 transfer 内部的 Exception 回退逻辑。"""


def test_crash_after_donor_write_then_restart_rolls_back(env):
    svc, cg, state = env
    svc.close()  # 释放 flock，模拟另一个进程

    def hook(stage):
        assert stage == "after_donor_write"
        raise SimulatedCrash

    crashed = make_service(cg, state, crash_hook=hook)
    crashed.recover()
    with pytest.raises(SimulatedCrash):
        crashed.transfer(B, 20000)
    # 崩溃现场：让出方已降额，接收方未升额，日志处于 in_progress
    assert quota(crashed, B) == 30000
    assert quota(crashed, A) == INIT_A
    entry = Journal(crashed.journal.path).read()
    assert entry["state"] == STATE_IN_PROGRESS
    crashed.close()

    # 重启：新实例恢复，回退未登记成功的转移
    restarted = make_service(cg, state)
    try:
        res = restarted.recover()
        assert res["result"] == "rolled_back"
        assert quota(restarted, A) == INIT_A and quota(restarted, B) == INIT_B
        assert restarted.recovered is True
        assert restarted.last_result["result"] == "rolled_back"
        # 恢复完成后可正常转移
        restarted.transfer(B, 10000)
        assert quota(restarted, A) == 110000 and quota(restarted, B) == 40000
    finally:
        restarted.close()


def test_rollback_writes_decreases_first(env):
    """重启回退时先降后升，全程总额不超过转移前总额。"""
    svc, cg, state = env
    svc.close()
    # 构造崩溃现场：两组都已写完（post），但未来得及登记
    write_cpu_max_file(cg / A / "cpu.max", 120000)
    write_cpu_max_file(cg / B / "cpu.max", 30000)
    journal = Journal(state / "transfer_journal.json")
    journal.write(
        {
            "state": STATE_IN_PROGRESS,
            "transfer_id": "t1",
            "pre": {
                A: {"quota": str(INIT_A), "period": PERIOD},
                B: {"quota": str(INIT_B), "period": PERIOD},
            },
        }
    )
    restarted = make_service(cg, state)
    try:
        orig = service_mod.write_cpu_max
        writes = []

        def spy(path, cm):
            orig(path, cm)
            writes.append((Path(path).parent.name, int(cm.quota)))

        service_mod.write_cpu_max = spy
        try:
            res = restarted.recover()
        finally:
            service_mod.write_cpu_max = orig
        assert res["result"] == "rolled_back"
        # 先降接收方 A（120000→100000），再升让出方 B（30000→50000）
        assert writes == [(A, 100000), (B, 50000)]
        assert quota(restarted, A) == INIT_A and quota(restarted, B) == INIT_B
    finally:
        restarted.close()


def test_registered_transfer_kept_on_restart(env):
    svc, cg, state = env
    svc.close()
    # 构造现场：两组已是新配额，日志为 registered（登记成功后、清理前崩溃）
    write_cpu_max_file(cg / A / "cpu.max", 120000)
    write_cpu_max_file(cg / B / "cpu.max", 30000)
    journal = Journal(state / "transfer_journal.json")
    journal.write(
        {
            "state": STATE_REGISTERED,
            "transfer_id": "t2",
            "pre": {
                A: {"quota": str(INIT_A), "period": PERIOD},
                B: {"quota": str(INIT_B), "period": PERIOD},
            },
        }
    )
    restarted = make_service(cg, state)
    try:
        res = restarted.recover()
        assert res["result"] == "kept_registered"
        # 保留新配额，不回退
        assert quota(restarted, A) == 120000 and quota(restarted, B) == 30000
        assert restarted.journal.read() is None
    finally:
        restarted.close()


def test_recovery_failure_keeps_rejecting(env):
    svc, cg, state = env
    svc.close()
    (state / "transfer_journal.json").write_text("这不是合法 JSON")
    restarted = make_service(cg, state)
    try:
        with pytest.raises(Exception):
            restarted.recover()
        assert restarted.recovered is False
        with pytest.raises(RecoveryPendingError):
            restarted.transfer(B, 1000)
        assert restarted.last_result["result"] == "failed"
    finally:
        restarted.close()


# ---------------------------------------------------------------- 结果持久化


def test_last_result_persisted_across_restart(env):
    svc, cg, state = env
    svc.recover()
    svc.transfer(B, 20000)
    svc.close()
    restarted = make_service(cg, state)
    try:
        assert restarted.last_result is not None
        assert restarted.last_result["result"] == "success"
        assert restarted.last_result["amount"] == 20000
    finally:
        restarted.close()


def test_exclusive_lock(env):
    svc, cg, state = env
    with pytest.raises(RuntimeError):
        make_service(cg, state)  # 同一状态目录不允许第二个实例

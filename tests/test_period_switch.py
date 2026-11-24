"""周期切换测试：短周期切换、精确换算、失败回退、崩溃撤回/登记保留、
基线单位划转、重启后核算单位不变。"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.service as service_mod
from app.cgroups import CpuMax, read_cpu_max
from app.journal import STATE_IN_PROGRESS, STATE_REGISTERED, Journal
from app.service import (
    ACTION_PERIOD_SWITCH,
    ACTION_TRANSFER,
    RecoveryPendingError,
    TransferError,
    TransferService,
    WriteFailureError,
)

A, B = "groupA", "groupB"
PERIOD = 100000
SHORT = 50000
MIN_Q = 20000
INIT_A, INIT_B = 100000, 50000


def write_cpu_max_file(path: Path, quota, period=PERIOD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{quota} {period}\n")


def make_service(cg, state, target_period=None, crash_hook=None) -> TransferService:
    return TransferService(
        {A: cg / A / "cpu.max", B: cg / B / "cpu.max"},
        state,
        MIN_Q,
        crash_hook=crash_hook,
        target_period=target_period,
    )


def raw(svc, g) -> str:
    return read_cpu_max(svc.group_paths[g]).render().strip()


@pytest.fixture
def env(tmp_path):
    cg = tmp_path / "cgroup"
    state = tmp_path / "state"
    write_cpu_max_file(cg / A / "cpu.max", INIT_A)
    write_cpu_max_file(cg / B / "cpu.max", INIT_B)
    yield cg, state


# ---------------------------------------------------------------- 启动切换


def test_switch_applied_at_startup(env):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        res = svc.recover()
        assert res["result"] == "switched"
        assert res["old_period"] == PERIOD and res["new_period"] == SHORT
        # 每组 quota/period 同比缩放，比值不变
        assert raw(svc, A) == "50000 50000"
        assert raw(svc, B) == "25000 50000"
        assert svc.period_switched is True
        assert svc.baseline_period == PERIOD
        assert svc.active_period == SHORT
        # 周期状态已持久化
        import json

        ps = json.loads((state / "period_state.json").read_text())
        assert ps == {
            "version": 1,
            "baseline_period": PERIOD,
            "active_period": SHORT,
            "switched": True,
        }
        # 日志已清理
        assert svc.journal.read() is None
    finally:
        svc.close()


def test_no_target_period_means_no_switch(env):
    cg, state = env
    svc = make_service(cg, state)
    try:
        res = svc.recover()
        assert res["result"] == "nothing_pending"
        assert raw(svc, A) == "100000 100000"
        assert raw(svc, B) == "50000 100000"
        assert svc.period_switched is False
        assert svc.baseline_period == PERIOD
    finally:
        svc.close()


def test_switch_each_write_keeps_group_ratio(env, monkeypatch):
    """切换过程中每次成功写入后，已写组保持原 quota/period 比值。"""
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    snapshots = []
    orig = service_mod.write_cpu_max

    def spy(path, cm):
        orig(path, cm)
        snapshots.append({g: read_cpu_max(p) for g, p in svc.group_paths.items()})

    monkeypatch.setattr(service_mod, "write_cpu_max", spy)
    try:
        svc.recover()
        # 第一次写入后：groupA 已切（100000/100000 == 50000/50000），groupB 未写仍为原值
        first = snapshots[0]
        assert first[A] == CpuMax("50000", SHORT)
        assert first[B] == CpuMax("50000", PERIOD)
        assert 50000 / SHORT == INIT_A / PERIOD
        # 第二次写入后：两组都已切，各自比值不变
        second = snapshots[1]
        assert second[B] == CpuMax("25000", SHORT)
        assert 25000 / SHORT == INIT_B / PERIOD
    finally:
        svc.close()


def test_inexact_conversion_rejects_switch_and_keeps_old_period(env):
    cg, state = env
    # 100000 -> period 30000: 100000*30000/100000 = 30000 整除；
    # 50000*30000/100000 = 15000 整除；换一个让组 B 不整除的：period 70000
    # 50000*70000/100000 = 35000 整除；用 period 30000 配合非整除初始值
    write_cpu_max_file(cg / B / "cpu.max", 50001)
    svc = make_service(cg, state, target_period=30000)
    try:
        res = svc.recover()
        assert res["result"] == "rejected"
        assert "无法精确换算" in res["error"]
        # 两组文件均未改动，仍按旧周期服务
        assert raw(svc, A) == "100000 100000"
        assert raw(svc, B) == "50001 100000"
        assert svc.period_switched is False
        assert svc.journal.read() is None
        # 恢复仍算完成，划转可用（旧周期）
        assert svc.recovered is True
        out = svc.transfer(B, 10000)
        assert out["result"] == "success"
        assert raw(svc, B) == "40001 100000"
    finally:
        svc.close()


def test_target_not_shorter_rejected(env):
    cg, state = env
    svc = make_service(cg, state, target_period=PERIOD)
    try:
        res = svc.recover()
        assert res["result"] == "nothing_pending"  # 目标即当前：无操作
        assert raw(svc, A).endswith(str(PERIOD))
    finally:
        svc.close()

    svc2 = make_service(cg, state, target_period=200000)
    try:
        res = svc2.recover()
        assert res["result"] == "rejected"
        assert "不短于" in res["error"]
        assert raw(svc2, A) == "100000 100000"
    finally:
        svc2.close()


def test_switch_write_failure_restores_both_groups(env, monkeypatch):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    orig = service_mod.write_cpu_max
    group_b_path = svc.group_paths[B]

    def flaky(path, cm):
        if Path(path) == group_b_path:
            raise OSError("模拟第二组写失败")
        return orig(path, cm)

    monkeypatch.setattr(service_mod, "write_cpu_max", flaky)
    try:
        res = svc.recover()
        assert res["result"] == "failed"
        assert res["rolled_back"] is True
        # 已还原两组，按旧周期服务
        assert raw(svc, A) == "100000 100000"
        assert raw(svc, B) == "50000 100000"
        assert svc.period_switched is False
        assert svc.journal.read() is None
        assert svc.recovered is True
    finally:
        monkeypatch.setattr(service_mod, "write_cpu_max", orig)
        svc.close()

    # 重启后配置仍在：上次是同进程写失败回退（非撤回），允许再次尝试，这次成功
    svc2 = make_service(cg, state, target_period=SHORT)
    try:
        res = svc2.recover()
        assert res["result"] == "switched"
        assert raw(svc2, A) == "50000 50000"
        assert raw(svc2, B) == "25000 50000"
    finally:
        svc2.close()


# ---------------------------------------------------------------- 崩溃与重启


class SimulatedCrash(BaseException):
    """模拟进程崩溃：绕过内部 Exception 回退逻辑。"""


def test_crash_during_switch_then_restart_rolls_back(env):
    cg, state = env

    def hook(stage):
        assert stage == "after_first_switch_write"
        raise SimulatedCrash

    crashed = make_service(cg, state, target_period=SHORT, crash_hook=hook)
    try:
        crashed.recover()
        pytest.fail("应当模拟崩溃退出")
    except SimulatedCrash:
        pass
    # 崩溃现场：groupA 已切换，groupB 未切换，日志 in_progress
    assert raw(crashed, A) == "50000 50000"
    assert raw(crashed, B) == "50000 100000"
    entry = Journal(crashed.journal.path).read()
    assert entry["state"] == STATE_IN_PROGRESS
    assert entry["action"] == ACTION_PERIOD_SWITCH
    crashed.close()

    # 重启（配置不变）：撤回未登记切换，本次启动按旧周期服务，不再重试
    restarted = make_service(cg, state, target_period=SHORT)
    try:
        res = restarted.recover()
        assert res["result"] == "rolled_back"
        assert raw(restarted, A) == "100000 100000"
        assert raw(restarted, B) == "50000 100000"
        assert restarted.period_switched is False
        assert restarted._switch_suppressed is True
        assert restarted.journal.read() is None
        # 撤回后本次启动不再切换：再次 recover 也无新动作
        again = restarted._try_startup_period_switch()
        assert again is None
    finally:
        restarted.close()

    # 再下一次重启：抑制标记只在当次启动生效，配置仍在，可以重新切换成功
    second = make_service(cg, state, target_period=SHORT)
    try:
        res = second.recover()
        assert res["result"] == "switched"
        assert raw(second, A) == "50000 50000"
        assert raw(second, B) == "25000 50000"
    finally:
        second.close()


def test_registered_switch_kept_on_restart(env):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        svc.recover()
        assert raw(svc, A) == "50000 50000"
    finally:
        svc.close()

    # 模拟“登记成功后、清理日志前崩溃”的现场
    journal = Journal(state / "transfer_journal.json")
    journal.write(
        {
            "version": 2,
            "action": ACTION_PERIOD_SWITCH,
            "state": STATE_REGISTERED,
            "transfer_id": "s1",
            "old_period": PERIOD,
            "new_period": SHORT,
            "pre": {
                A: {"quota": str(INIT_A), "period": PERIOD},
                B: {"quota": str(INIT_B), "period": PERIOD},
            },
            "post": {
                A: {"quota": "50000", "period": SHORT},
                B: {"quota": "25000", "period": SHORT},
            },
        }
    )
    restarted = make_service(cg, state, target_period=SHORT)
    try:
        res = restarted.recover()
        assert res["result"] == "kept_registered"
        # 已登记的切换保留：不回退、不重复切换
        assert raw(restarted, A) == "50000 50000"
        assert raw(restarted, B) == "25000 50000"
        assert restarted.period_switched is True
        assert restarted.baseline_period == PERIOD
        assert restarted.active_period == SHORT
        assert restarted.journal.read() is None
    finally:
        restarted.close()


def test_restart_without_config_keeps_switched_period(env):
    """切换成功后，即使重启时不带 CPU_PERIOD_USEC，短周期与基线单位仍保留。"""
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        svc.recover()
    finally:
        svc.close()

    restarted = make_service(cg, state, target_period=None)
    try:
        res = restarted.recover()
        assert res["result"] == "nothing_pending"
        assert raw(restarted, A) == "50000 50000"
        assert restarted.baseline_period == PERIOD
        assert restarted.active_period == SHORT
    finally:
        restarted.close()


# ---------------------------------------------------------------- 切换后按基线单位划转


def test_transfer_after_switch_uses_baseline_units(env):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        svc.recover()
        # 基线单位划转 20000（period 100000）→ 当前周期 delta = 10000
        out = svc.transfer(B, 20000)
        assert out["amount"] == 20000
        assert out["amount_period"] == PERIOD
        assert out["applied_delta"] == 10000
        assert out["active_period"] == SHORT
        assert raw(svc, A) == "60000 50000"
        assert raw(svc, B) == "15000 50000"
        # 总额（折回基线周期）守恒：60000*2 + 15000*2 = 150000
        total_base = sum(
            svc._exact_scale(read_cpu_max(p).quota_value, SHORT, PERIOD)
            for p in svc.group_paths.values()
        )
        assert total_base == INIT_A + INIT_B
    finally:
        svc.close()


def test_min_quota_always_accounted_in_baseline_units(env):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        svc.recover()
        # B 当前 25000@50000（=50000 基线单位），下限 20000 基线单位
        # 让出 30002 基线单位（delta=15001，整除）→ 剩 19998 < 20000，拒绝
        with pytest.raises(TransferError, match="下限"):
            svc.transfer(B, 30002)
        # 让出 30000 基线单位 → 剩 20000，恰好允许；delta=15000
        out = svc.transfer(B, 30000)
        assert out["applied_delta"] == 15000
        assert raw(svc, B) == "10000 50000"
    finally:
        svc.close()


def test_inexact_transfer_amount_rejected(env):
    cg, state = env
    # period 100000 -> 30000：100000->30000、50000->15000 都整除
    svc = make_service(cg, state, target_period=30000)
    try:
        svc.recover()
        assert raw(svc, A) == "30000 30000"
        assert raw(svc, B) == "15000 30000"
        # amount=1000 基线单位：1000*30000/100000 = 300 整除，允许
        ok = svc.transfer(B, 1000)
        assert ok["applied_delta"] == 300
        # amount=100 基线单位：100*30000/100000 = 30 整除
        ok2 = svc.transfer(B, 100)
        assert ok2["applied_delta"] == 30
        # amount=101：3030/100 ... 101*30000/100000 = 30.3 非整数，拒单
        with pytest.raises(TransferError, match="无法精确换算"):
            svc.transfer(B, 101)
        # 拒单不动文件
        assert raw(svc, B) == "14670 30000"
    finally:
        svc.close()


def test_baseline_persists_across_restarts_with_transfers(env):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        svc.recover()
        svc.transfer(B, 20000)  # B: 25000 -> 15000
    finally:
        svc.close()

    restarted = make_service(cg, state, target_period=SHORT)
    try:
        restarted.recover()
        assert restarted.baseline_period == PERIOD  # 核算单位不变
        out = restarted.transfer(A, 10000)  # 基线 10000 -> delta 5000
        assert out["applied_delta"] == 5000
        assert raw(restarted, A) == "55000 50000"
        assert raw(restarted, B) == "20000 50000"
    finally:
        restarted.close()


def test_switch_then_transfer_crash_recovery_still_works(env):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    try:
        svc.recover()
    finally:
        svc.close()

    def hook(stage):
        raise SimulatedCrash

    crashed = make_service(cg, state, target_period=SHORT, crash_hook=hook)
    try:
        crashed.recover()
        with pytest.raises(SimulatedCrash):
            crashed.transfer(B, 10000)  # delta=5000
    finally:
        crashed.close()
    # 现场：B 已降（25000->20000），A 未升
    restarted = make_service(cg, state, target_period=SHORT)
    try:
        res = restarted.recover()
        assert res["action"] == ACTION_TRANSFER
        assert res["result"] == "rolled_back"
        assert raw(restarted, A) == "50000 50000"
        assert raw(restarted, B) == "25000 50000"
    finally:
        restarted.close()


def test_transfer_write_failure_after_switch_restores(env, monkeypatch):
    cg, state = env
    svc = make_service(cg, state, target_period=SHORT)
    orig = service_mod.write_cpu_max
    try:
        svc.recover()
        receiver_path = svc.group_paths[A]

        def flaky(path, cm):
            if Path(path) == receiver_path:
                raise OSError("模拟接收方写失败")
            return orig(path, cm)

        service_mod.write_cpu_max = flaky
        with pytest.raises(WriteFailureError):
            svc.transfer(B, 20000)
        assert raw(svc, A) == "50000 50000"
        assert raw(svc, B) == "25000 50000"
    finally:
        service_mod.write_cpu_max = orig
        svc.close()


def test_recovery_pending_blocks_transfer_during_switch_failure(env, monkeypatch):
    """恢复阶段本身抛错（如日志损坏）时，切换不执行且转移被拒。"""
    cg, state = env
    state.mkdir(exist_ok=True)
    (state / "transfer_journal.json").write_text("损坏的日志")
    svc = make_service(cg, state, target_period=SHORT)
    try:
        with pytest.raises(Exception):
            svc.recover()
        assert svc.recovered is False
        with pytest.raises(RecoveryPendingError):
            svc.transfer(B, 1000)
        # 文件未被切换触碰
        assert raw(svc, A) == "100000 100000"
    finally:
        svc.close()

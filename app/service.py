"""CPU 配额划转核心逻辑。

不变式（转移与 2 组写文件的全过程中都必须成立）：
  1. 两组 quota 之和不超过本次操作前的总额；
  2. 任一组 quota（按基线周期折算后）不低于设定下限 min_quota。

实现手段：任何涉及两组写入的操作都“先降后升”——
  * 正常转移：先写让出方（降额），再写接收方（升额）；
  * 失败回退 / 重启回退：先写需要降的组，再写需要升的组。
由于转移本身保持总额不变（让出方减多少、接收方加多少），
先降后升即可保证任意中间时刻总额都不超过操作前总额，
且各组取值始终介于“操作前/后”两个合法值之间，不会低于下限。

周期切换（可选启动配置 CPU_PERIOD_USEC）：
  启动时可把两组 cpu.max 从当前（较长）周期切到指定的较短周期。
  切换/回退的每次成功写入后，两组 quota/period 都保持各自原值
  （即 quota 与 period 同比缩放，CPU 时间占比不变）。
  配额不能精确换算成整数时拒绝切换；单次写入失败即还原两组，按旧周期服务。
  切换同样走 WAL 状态机：崩溃后重启时，未登记（in_progress）的切换被撤回，
  本次启动按旧周期服务；已登记（registered）的切换保留。

核算单位：
  /transfers 的 amount 与 MIN_QUOTA 始终按“首次切换前的周期”（基线周期）
  核算，并持久化在 state/period_state.json 中，重启后不变。
  划转量无法按当前周期精确换算成整数 quota 时拒单。
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .cgroups import CpuMax, CpuMaxError, read_cpu_max, write_cpu_max
from .journal import STATE_IN_PROGRESS, STATE_REGISTERED, Journal, atomic_write_json

ACTION_TRANSFER = "transfer"
ACTION_PERIOD_SWITCH = "period_switch"


class TransferError(Exception):
    """业务校验失败（请求非法），对应 HTTP 400。"""


class RecoveryPendingError(Exception):
    """启动恢复尚未完成，拒绝新转移，对应 HTTP 503。"""


class WriteFailureError(Exception):
    """写入 cpu.max 失败（已回退到操作前配额），对应 HTTP 500。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TransferService:
    """独占管理两组 cpu.max 的划转服务。"""

    def __init__(
        self,
        group_paths: dict[str, Path],
        state_dir: Path,
        min_quota: int,
        crash_hook: Callable[[str], None] | None = None,
        target_period: int | None = None,
    ):
        if len(group_paths) != 2:
            raise ValueError("必须且只能管理两组 cpu.max")
        self.group_paths = {k: Path(v) for k, v in group_paths.items()}
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # min_quota 以“首次切换前的基线周期”为单位，全程不变
        self.min_quota = int(min_quota)
        # 目标短周期：None 表示不配置周期切换；整个生命周期至多成功切换一次
        self.target_period = None if target_period is None else int(target_period)
        self.journal = Journal(self.state_dir / "transfer_journal.json")
        self._last_result_path = self.state_dir / "last_result.json"
        self._period_state_path = self.state_dir / "period_state.json"
        self._lock = threading.Lock()
        # crash_hook：演示/测试用，模拟“首个组写完后进程崩溃”。生产为 None。
        self._crash_hook = crash_hook
        self.recovered = False
        self.last_result: dict | None = self._load_json(self._last_result_path)
        # 周期状态（从 state/period_state.json 读取或惰性建立）：
        #   baseline_period  首次切换前的周期，amount/MIN_QUOTA 的核算单位
        #   active_period    当前生效周期
        #   switched         是否已成功登记过周期切换（一生至多一次）
        self._period_state: dict | None = self._load_json(self._period_state_path)
        # 本次启动是否撤回过未登记的切换；撤回后本次启动不再尝试切换
        self._switch_suppressed = False
        self._lock_fd: int | None = self._acquire_exclusive_lock()

    # ------------------------------------------------------------------
    # 独占管理：进程内互斥锁 + 状态目录上的 flock，防止并发/多实例写同一组文件
    # ------------------------------------------------------------------
    def _acquire_exclusive_lock(self) -> int:
        fd = os.open(self.state_dir / "service.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise RuntimeError(f"状态目录 {self.state_dir} 已被其他服务实例锁定") from e
        return fd

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    # ------------------------------------------------------------------
    # 周期状态与单位换算
    # ------------------------------------------------------------------
    @property
    def baseline_period(self) -> int:
        """首次切换前的周期；周期状态尚未建立时取磁盘上的当前周期。"""
        if self._period_state is not None:
            return int(self._period_state["baseline_period"])
        return self._current_period()

    @property
    def active_period(self) -> int:
        if self._period_state is not None:
            return int(self._period_state["active_period"])
        return self._current_period()

    @property
    def period_switched(self) -> bool:
        return bool(self._period_state and self._period_state.get("switched"))

    def _current_period(self) -> int:
        periods = {read_cpu_max(p).period for p in self.group_paths.values()}
        if len(periods) != 1:
            raise TransferError(
                f"两组 period 不一致: {sorted(periods)}"
            )
        return periods.pop()

    def _ensure_period_state(self, current: dict[str, CpuMax]) -> None:
        """惰性建立周期状态：基线周期 = 首次切换前的磁盘周期。"""
        if self._period_state is None:
            period = current[next(iter(current))].period
            self._period_state = {
                "version": 1,
                "baseline_period": period,
                "active_period": period,
                "switched": False,
            }
            self._save_period_state()

    def _save_period_state(self) -> None:
        atomic_write_json(self._period_state_path, self._period_state)

    @staticmethod
    def _exact_scale(value: int, from_period: int, to_period: int) -> int:
        """把 from_period 周期下的 quota 精确换算到 to_period 周期。

        无法整除（换算不到整数）时抛 TransferError。
        """
        numerator = value * to_period
        if numerator % from_period != 0:
            raise TransferError(
                f"配额 {value}（period {from_period}）无法精确换算为 period "
                f"{to_period} 下的整数 quota（{numerator}/{from_period} 非整数）"
            )
        return numerator // from_period

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def quotas(self) -> dict:
        out = {}
        for name, path in self.group_paths.items():
            cm = read_cpu_max(path)
            out[name] = {"quota": cm.quota, "period": cm.period, "raw": cm.render().strip()}
        out["periods"] = {
            "baseline_period": self.baseline_period,
            "active_period": self._current_period(),
        }
        return out

    def status(self) -> dict:
        return {
            "recovered": self.recovered,
            "min_quota": self.min_quota,
            "min_quota_period": self.baseline_period,
            "period_switch": {
                "configured_target_period": self.target_period,
                "baseline_period": self.baseline_period,
                "active_period": self._safe_current_period(),
                "switched": self.period_switched,
                "suppressed_after_rollback": self._switch_suppressed,
            },
            "last_result": self.last_result,
        }

    def _safe_current_period(self) -> int | None:
        try:
            return self._current_period()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 启动恢复：回退未登记成功的操作，保留已登记成功的新配额；
    # 随后按可选启动配置执行周期切换
    # ------------------------------------------------------------------
    def recover(self) -> dict:
        with self._lock:
            try:
                entry = self.journal.read()
                if entry is not None:
                    action = entry.get("action", ACTION_TRANSFER)
                    if entry.get("state") == STATE_IN_PROGRESS:
                        pre = {
                            g: CpuMax(str(v["quota"]), int(v["period"]))
                            for g, v in entry["pre"].items()
                        }
                        self._apply_targets_decrease_first(pre)
                        self.journal.clear()
                        if action == ACTION_PERIOD_SWITCH:
                            # 未登记的周期切换：撤回，本次启动按旧周期服务，不再重试
                            self._switch_suppressed = True
                            result = {
                                "action": ACTION_PERIOD_SWITCH,
                                "result": "rolled_back",
                                "transfer_id": entry.get("transfer_id"),
                                "restored": {g: c.render().strip() for g, c in pre.items()},
                                "at": _now(),
                            }
                        else:
                            result = {
                                "action": ACTION_TRANSFER,
                                "result": "rolled_back",
                                "transfer_id": entry.get("transfer_id"),
                                "restored": {g: c.render().strip() for g, c in pre.items()},
                                "at": _now(),
                            }
                    elif entry.get("state") == STATE_REGISTERED:
                        # 已登记成功：保留磁盘上的新配额，仅清理日志
                        self.journal.clear()
                        result = {
                            "action": action,
                            "result": "kept_registered",
                            "transfer_id": entry.get("transfer_id"),
                            "at": _now(),
                        }
                    else:
                        raise CpuMaxError(f"日志状态未知: {entry.get('state')!r}")
                    self.recovered = True
                    self._record_result(result)
                else:
                    # 无未决操作：不动配额，也不覆盖上次处理结果
                    self.recovered = True

                # 按启动配置执行周期切换（与恢复互斥、串行；切换失败不影响 recovered）
                switch_result = self._try_startup_period_switch()
                if switch_result is not None:
                    result = switch_result
            except Exception as e:
                # 恢复失败：recovered 保持 False，新转移继续被拒绝
                self._record_result(
                    {"action": "recovery", "result": "failed", "error": str(e), "at": _now()}
                )
                raise
            return result if "result" in locals() else {
                "action": "recovery",
                "result": "nothing_pending",
                "at": _now(),
            }

    # ------------------------------------------------------------------
    # 周期切换（仅启动时、在服务锁内执行）
    # ------------------------------------------------------------------
    def _try_startup_period_switch(self) -> dict | None:
        """按启动配置尝试切换到较短周期。

        返回 None 表示本次启动无需切换（未配置、已切换、已撤回、目标即当前）。
        切换被拒绝（换算非整数、目标不更短等）或写入失败已回退时，
        返回 result != "switched" 的结果字典，并按旧周期继续服务。
        """
        if self.target_period is None:
            return None
        if self.period_switched:
            return None  # 整个生命周期至多成功切换一次
        if self._switch_suppressed:
            return None  # 本次启动已撤回过未登记切换，按旧周期服务

        current = {g: read_cpu_max(p) for g, p in self.group_paths.items()}
        self._ensure_period_state(current)
        new_period = self.target_period
        old_period = current[next(iter(current))].period

        if new_period == old_period:
            return None
        if new_period <= 0:
            return self._switch_rejected(f"目标 period 非法: {new_period}")
        if new_period >= old_period:
            return self._switch_rejected(
                f"目标 period {new_period} 不短于当前 period {old_period}，拒绝切换"
            )
        periods = {c.period for c in current.values()}
        if len(periods) != 1:
            return self._switch_rejected(
                f"两组 period 不一致: {[c.period for c in current.values()]}"
            )
        for g, c in current.items():
            if c.is_max:
                return self._switch_rejected(f"{g} 的 quota 为 max，无法换算切换")

        # 先做全部精确性校验：任一组换算不到整数即整体拒绝切换，不动任何文件
        new_values: dict[str, int] = {}
        try:
            for g, c in current.items():
                new_values[g] = self._exact_scale(
                    c.quota_value, old_period, new_period
                )
        except TransferError as e:
            return self._switch_rejected(str(e))

        post = {g: CpuMax(str(new_values[g]), new_period) for g in current}
        switch_id = uuid.uuid4().hex
        entry = {
            "version": 2,
            "action": ACTION_PERIOD_SWITCH,
            "state": STATE_IN_PROGRESS,
            "transfer_id": switch_id,
            "old_period": old_period,
            "new_period": new_period,
            "pre": {
                g: {"quota": c.quota, "period": c.period} for g, c in current.items()
            },
            "post": {
                g: {"quota": c.quota, "period": c.period} for g, c in post.items()
            },
            "created_at": _now(),
        }
        self.journal.write(entry)

        try:
            # 两组依次写入；每个成功写入点上，已写组与未写组各自保持
            # 原 quota/period 比值（两组同比缩放），失败则整体还原
            ordered = sorted(self.group_paths)
            for index, g in enumerate(ordered):
                write_cpu_max(self.group_paths[g], post[g])
                if self._crash_hook is not None and index == 0:
                    self._crash_hook("after_first_switch_write")
        except Exception as e:
            # 单次写入失败：还原两组到旧周期（先降后升），按旧周期服务
            self._apply_targets_decrease_first(current)
            self.journal.clear()
            result = {
                "action": ACTION_PERIOD_SWITCH,
                "result": "failed",
                "transfer_id": switch_id,
                "old_period": old_period,
                "target_period": new_period,
                "error": str(e),
                "rolled_back": True,
                "at": _now(),
            }
            self._record_result(result)
            return result

        # 两组都写好后才登记成功；登记后更新周期状态并清理日志
        entry["state"] = STATE_REGISTERED
        entry["registered_at"] = _now()
        self.journal.write(entry)
        self._period_state["active_period"] = new_period
        self._period_state["switched"] = True
        self._save_period_state()
        self.journal.clear()

        result = {
            "action": ACTION_PERIOD_SWITCH,
            "result": "switched",
            "transfer_id": switch_id,
            "old_period": old_period,
            "new_period": new_period,
            "post": {g: c.render().strip() for g, c in post.items()},
            "at": _now(),
        }
        self._record_result(result)
        return result

    def _switch_rejected(self, reason: str) -> dict:
        result = {
            "action": ACTION_PERIOD_SWITCH,
            "result": "rejected",
            "target_period": self.target_period,
            "error": reason,
            "at": _now(),
        }
        self._record_result(result)
        return result

    # ------------------------------------------------------------------
    # 转移
    # ------------------------------------------------------------------
    def transfer(self, donor: str, amount: int) -> dict:
        with self._lock:
            if not self.recovered:
                raise RecoveryPendingError("启动恢复未完成，暂时拒绝新转移")
            if donor not in self.group_paths:
                raise TransferError(
                    f"未知让出方 {donor!r}，可选: {sorted(self.group_paths)}"
                )
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise TransferError("划转量必须为正整数")
            receiver = next(g for g in self.group_paths if g != donor)

            current = {g: read_cpu_max(p) for g, p in self.group_paths.items()}
            self._ensure_period_state(current)
            # amount 以基线周期为单位：精确换算到当前周期后再做划转
            baseline = self._period_state["baseline_period"]
            active = self._period_state["active_period"]
            delta = self._exact_scale(amount, baseline, active)
            self._check_transferable(current, donor, delta)

            post = dict(current)
            post[donor] = CpuMax(
                str(current[donor].quota_value - delta), current[donor].period
            )
            post[receiver] = CpuMax(
                str(current[receiver].quota_value + delta), current[receiver].period
            )

            transfer_id = uuid.uuid4().hex
            entry = {
                "version": 2,
                "action": ACTION_TRANSFER,
                "state": STATE_IN_PROGRESS,
                "transfer_id": transfer_id,
                "donor": donor,
                "receiver": receiver,
                "amount": amount,
                "amount_period": baseline,
                "applied_delta": delta,
                "pre": {
                    g: {"quota": c.quota, "period": c.period} for g, c in current.items()
                },
                "post": {
                    g: {"quota": c.quota, "period": c.period} for g, c in post.items()
                },
                "created_at": _now(),
            }
            self.journal.write(entry)

            try:
                # 先降（让出方）后升（接收方）：任意时刻两组之和都不超过转移前总额
                write_cpu_max(self.group_paths[donor], post[donor])
                if self._crash_hook is not None:
                    self._crash_hook("after_donor_write")
                write_cpu_max(self.group_paths[receiver], post[receiver])
            except Exception as e:
                # 写入失败：恢复本次转移前的配额（先降后升），清理日志
                self._apply_targets_decrease_first(current)
                self.journal.clear()
                self._record_result(
                    {
                        "action": ACTION_TRANSFER,
                        "result": "failed",
                        "transfer_id": transfer_id,
                        "donor": donor,
                        "receiver": receiver,
                        "amount": amount,
                        "error": str(e),
                        "rolled_back": True,
                        "at": _now(),
                    }
                )
                raise WriteFailureError(
                    f"写入 cpu.max 失败，已恢复转移前配额: {e}"
                ) from e

            # 两组都写好后才登记成功；登记成功后即可安全清理日志
            entry["state"] = STATE_REGISTERED
            entry["registered_at"] = _now()
            self.journal.write(entry)
            self.journal.clear()

            result = {
                "action": ACTION_TRANSFER,
                "result": "success",
                "transfer_id": transfer_id,
                "donor": donor,
                "receiver": receiver,
                "amount": amount,
                "amount_period": baseline,
                "applied_delta": delta,
                "active_period": active,
                "post": {g: c.render().strip() for g, c in post.items()},
                "at": _now(),
            }
            self._record_result(result)
            return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _check_transferable(
        self, current: dict[str, CpuMax], donor: str, delta: int
    ) -> None:
        periods = {c.period for c in current.values()}
        if len(periods) != 1:
            raise TransferError(
                f"两组 period 不一致: {[c.period for c in current.values()]}"
            )
        active = periods.pop()
        for g, c in current.items():
            if c.is_max:
                raise TransferError(f"{g} 的 quota 为 max，无法参与数值划转")
        donor_new = current[donor].quota_value - delta
        # 下限按基线周期核算：把让出方划转后的当前周期 quota 折回基线周期比较
        baseline = self._period_state["baseline_period"]
        donor_new_base = self._exact_scale(donor_new, active, baseline)
        if donor_new_base < self.min_quota:
            raise TransferError(
                f"让出方 {donor} 划转后为 {donor_new_base}（period {baseline}），"
                f"低于下限 {self.min_quota}"
            )

    def _apply_targets_decrease_first(self, targets: dict[str, CpuMax]) -> None:
        """把两组 cpu.max 调整为 targets。

        先写变小的组、再写变大的组：目标总额等于操作前总额，
        因此回退全过程中两组之和也不超过操作前总额，且各组不会低于下限
        （目标值即操作前的合法值）。
        """
        current = {g: read_cpu_max(p) for g, p in self.group_paths.items()}

        def delta(g: str) -> int:
            try:
                return targets[g].quota_value - current[g].quota_value
            except CpuMaxError:
                return 0

        for g in sorted(self.group_paths, key=delta):
            if current[g] != targets[g]:
                write_cpu_max(self.group_paths[g], targets[g])

    def _record_result(self, result: dict) -> None:
        self.last_result = result
        atomic_write_json(self._last_result_path, result)

    @staticmethod
    def _load_json(path: Path) -> dict | None:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

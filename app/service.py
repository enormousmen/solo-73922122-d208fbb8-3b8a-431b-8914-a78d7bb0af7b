"""CPU 配额划转核心逻辑。

不变式（转移与 2 组写文件的全过程中都必须成立）：
  1. 两组 quota 之和不超过本次转移前的总额；
  2. 任一组 quota 不低于设定下限 min_quota。

实现手段：任何涉及两组写入的操作都“先降后升”——
  * 正常转移：先写让出方（降额），再写接收方（升额）；
  * 失败回退 / 重启回退：先写需要降的组，再写需要升的组。
由于转移本身保持总额不变（让出方减多少、接收方加多少），
先降后升即可保证任意中间时刻总额都不超过转移前总额，
且各组取值始终介于“转移前/后”两个合法值之间，不会低于下限。
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


class TransferError(Exception):
    """业务校验失败（请求非法），对应 HTTP 400。"""


class RecoveryPendingError(Exception):
    """启动恢复尚未完成，拒绝新转移，对应 HTTP 503。"""


class WriteFailureError(Exception):
    """写入 cpu.max 失败（已回退到转移前配额），对应 HTTP 500。"""


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
    ):
        if len(group_paths) != 2:
            raise ValueError("必须且只能管理两组 cpu.max")
        self.group_paths = {k: Path(v) for k, v in group_paths.items()}
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.min_quota = int(min_quota)
        self.journal = Journal(self.state_dir / "transfer_journal.json")
        self._last_result_path = self.state_dir / "last_result.json"
        self._lock = threading.Lock()
        # crash_hook：演示/测试用，模拟“让出方降额后进程崩溃”。生产为 None。
        self._crash_hook = crash_hook
        self.recovered = False
        self.last_result: dict | None = self._load_json(self._last_result_path)
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
    # 查询
    # ------------------------------------------------------------------
    def quotas(self) -> dict:
        out = {}
        for name, path in self.group_paths.items():
            cm = read_cpu_max(path)
            out[name] = {"quota": cm.quota, "period": cm.period, "raw": cm.render().strip()}
        return out

    def status(self) -> dict:
        return {
            "recovered": self.recovered,
            "min_quota": self.min_quota,
            "last_result": self.last_result,
        }

    # ------------------------------------------------------------------
    # 启动恢复：回退未登记成功的转移，保留已登记成功的新配额
    # ------------------------------------------------------------------
    def recover(self) -> dict:
        with self._lock:
            try:
                entry = self.journal.read()
                if entry is None:
                    # 无未决转移：不动配额，也不覆盖上次处理结果
                    self.recovered = True
                    return {"action": "recovery", "result": "nothing_pending", "at": _now()}
                if entry.get("state") == STATE_IN_PROGRESS:
                    pre = {
                        g: CpuMax(str(v["quota"]), int(v["period"]))
                        for g, v in entry["pre"].items()
                    }
                    self._apply_targets_decrease_first(pre)
                    self.journal.clear()
                    result = {
                        "action": "recovery",
                        "result": "rolled_back",
                        "transfer_id": entry.get("transfer_id"),
                        "restored": {g: c.render().strip() for g, c in pre.items()},
                        "at": _now(),
                    }
                elif entry.get("state") == STATE_REGISTERED:
                    # 已登记成功：保留磁盘上的新配额，仅清理日志
                    self.journal.clear()
                    result = {
                        "action": "recovery",
                        "result": "kept_registered",
                        "transfer_id": entry.get("transfer_id"),
                        "at": _now(),
                    }
                else:
                    raise CpuMaxError(f"日志状态未知: {entry.get('state')!r}")
            except Exception as e:
                # 恢复失败：recovered 保持 False，新转移继续被拒绝
                self._record_result(
                    {"action": "recovery", "result": "failed", "error": str(e), "at": _now()}
                )
                raise
            self.recovered = True
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
            self._check_transferable(current, donor, amount)

            post = dict(current)
            post[donor] = CpuMax(
                str(current[donor].quota_value - amount), current[donor].period
            )
            post[receiver] = CpuMax(
                str(current[receiver].quota_value + amount), current[receiver].period
            )

            transfer_id = uuid.uuid4().hex
            entry = {
                "version": 1,
                "state": STATE_IN_PROGRESS,
                "transfer_id": transfer_id,
                "donor": donor,
                "receiver": receiver,
                "amount": amount,
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
                        "action": "transfer",
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
                "action": "transfer",
                "result": "success",
                "transfer_id": transfer_id,
                "donor": donor,
                "receiver": receiver,
                "amount": amount,
                "post": {g: c.render().strip() for g, c in post.items()},
                "at": _now(),
            }
            self._record_result(result)
            return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _check_transferable(
        self, current: dict[str, CpuMax], donor: str, amount: int
    ) -> None:
        periods = {c.period for c in current.values()}
        if len(periods) != 1:
            raise TransferError(
                f"两组 period 不一致: {[c.period for c in current.values()]}"
            )
        for g, c in current.items():
            if c.is_max:
                raise TransferError(f"{g} 的 quota 为 max，无法参与数值划转")
        donor_new = current[donor].quota_value - amount
        if donor_new < self.min_quota:
            raise TransferError(
                f"让出方 {donor} 划转后为 {donor_new}，低于下限 {self.min_quota}"
            )

    def _apply_targets_decrease_first(self, targets: dict[str, CpuMax]) -> None:
        """把两组 cpu.max 调整为 targets。

        先写变小的组、再写变大的组：目标总额等于转移前总额，
        因此回退全过程中两组之和也不超过转移前总额，且各组不会低于下限
        （目标值即转移前的合法值）。
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

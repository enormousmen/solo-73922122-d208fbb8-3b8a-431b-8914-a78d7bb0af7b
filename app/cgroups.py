"""读写 cgroup v2 cpu.max。

交付环境中用普通文件模拟（Docker Compose 把普通文件挂进容器），
接口与真实 /sys/fs/cgroup/<group>/cpu.max 完全一致：内容为
"<quota> <period>"，quota 为微秒数或 "max"。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MAX_QUOTA = "max"


class CpuMaxError(Exception):
    """cpu.max 内容非法或无法参与数值运算。"""


@dataclass(frozen=True)
class CpuMax:
    quota: str  # 数字字符串或 "max"
    period: int

    @property
    def is_max(self) -> bool:
        return self.quota == MAX_QUOTA

    @property
    def quota_value(self) -> int:
        if self.is_max:
            raise CpuMaxError("quota 为 max，无法参与数值划转")
        return int(self.quota)

    def render(self) -> str:
        return f"{self.quota} {self.period}\n"


def parse_cpu_max(text: str) -> CpuMax:
    parts = text.split()
    if len(parts) != 2:
        raise CpuMaxError(f"cpu.max 格式非法: {text!r}")
    quota, period_s = parts
    if quota != MAX_QUOTA and not quota.isdigit():
        raise CpuMaxError(f"quota 非法: {quota!r}")
    if not period_s.isdigit() or int(period_s) <= 0:
        raise CpuMaxError(f"period 非法: {period_s!r}")
    return CpuMax(quota=quota, period=int(period_s))


def read_cpu_max(path: Path) -> CpuMax:
    with open(path, "r") as f:
        return parse_cpu_max(f.read())


def write_cpu_max(path: Path, cpu_max: CpuMax) -> None:
    """写入并 fsync，模拟对 cgroup 接口文件的持久写。"""
    with open(path, "w") as f:
        f.write(cpu_max.render())
        f.flush()
        os.fsync(f.fileno())

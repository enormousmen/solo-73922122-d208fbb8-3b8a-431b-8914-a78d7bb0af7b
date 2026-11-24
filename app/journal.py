"""转移日志（WAL）与状态文件的原子持久化。

日志状态机：
  in_progress  —— 已记录转移意图与转移前配额，cgroup 写入进行中
  registered   —— 两组 cgroup 均已写好，转移“登记成功”
  （文件删除） —— 无未决转移

重启恢复时：in_progress 一律回退到日志中记录的转移前配额；
registered 保留新配额并清理日志。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_IN_PROGRESS = "in_progress"
STATE_REGISTERED = "registered"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """写临时文件 + fsync + rename + fsync 目录，保证崩溃后不出现半写文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        with open(self.path) as f:
            return json.load(f)

    def write(self, payload: dict) -> None:
        atomic_write_json(self.path, payload)

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

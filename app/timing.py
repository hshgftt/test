"""查询耗时埋点。

把一次请求拆成几段分别计时，输出形如::

    [timing] compare  db=1.842s  serialize=0.093s  total=1.941s  rows=168→2车

设计原则
--------
* **零依赖、可关闭**：`TIMING=0` 时全部变成 no-op，不留开销。
* **不吞异常**：纯粹包一层计时，语义不变。
* **能分阶段**：一次查询里往往既有 SQL 又有序列化，分开计时才知道慢在哪。

Python 3.8 兼容。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, Optional

ENABLED = os.getenv("TIMING", "1").strip().lower() not in ("0", "false", "no")


class Timer(object):
    """分段计时器。

    用法::

        t = Timer("compare")
        with t.stage("db"):
            rows = query()
        with t.stage("serialize"):
            payload = build(rows)
        t.finish(rows=len(rows))
    """

    __slots__ = ("label", "t0", "stages", "marks")

    def __init__(self, label: str = ""):
        self.label = label
        self.t0 = time.perf_counter()
        self.stages: Dict[str, float] = {}
        self.marks: Dict[str, Any] = {}

    # ── 分段 ──
    def stage(self, name: str) -> "_Stage":
        return _Stage(self, name)

    # ── 打点（记录任意附加信息，输出时带出来）──
    def mark(self, **kwargs: Any) -> None:
        self.marks.update(kwargs)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.t0

    def finish(self, log: bool = True, **marks: Any) -> float:
        self.mark(**marks)
        total = self.elapsed
        if log and ENABLED:
            self._log(total)
        return total

    def _log(self, total: float) -> None:
        parts = "  ".join(
            "%s=%.3fs" % (k, v) for k, v in self.stages.items()
        )
        extra = ""
        if self.marks:
            extra = "  " + "  ".join(
                "%s=%s" % (k, v) for k, v in self.marks.items()
            )
        msg = "[timing] %-14s %s  total=%.3fs%s" % (
            self.label, parts, total, extra,
        )
        print(msg, file=sys.stderr, flush=True)

    def summary(self) -> str:
        """给响应头用的紧凑格式（不含空格/中文，避免头编码问题）。"""
        items = ["total=%.3f" % self.elapsed]
        items += ["%s=%.3f" % (k, v) for k, v in self.stages.items()]
        return "; ".join(items)


class _Stage(object):
    """`with timer.stage('db'):` 的上下文管理器。"""

    __slots__ = ("timer", "name", "t0")

    def __init__(self, timer: Timer, name: str):
        self.timer = timer
        self.name = name

    def __enter__(self) -> "_Stage":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        dt = time.perf_counter() - self.t0
        # 同名分段累加（例如循环里多次查询）
        self.timer.stages[self.name] = self.timer.stages.get(self.name, 0.0) + dt


def fmt_size(n: Optional[int]) -> str:
    """字节数转可读字符串，用于日志。"""
    if not n:
        return "0B"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return "%.1f%s" % (f, unit)
        f /= 1024.0
    return "%.1fGB" % f

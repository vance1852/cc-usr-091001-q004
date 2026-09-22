"""时钟抽象：演练用虚拟时钟，实盘用系统时钟。

协调器只通过时钟读取当前时间，因此演练可以按设备响应的 delay
精确推进时间，超时判定在测试与实盘中的行为完全一致。
"""

from __future__ import annotations

import time


class VirtualClock:
    """单调递增的虚拟时钟，供演练与测试显式推进。"""

    def __init__(self, start: float = 0.0):
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, to: float) -> float:
        to = float(to)
        if to < self._now:
            raise ValueError(f"虚拟时钟不可回拨：{self._now} -> {to}")
        self._now = to
        return self._now


class SystemClock:
    def now(self) -> float:
        return time.time()

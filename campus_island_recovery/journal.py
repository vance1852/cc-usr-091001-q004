"""追加式事件 journal：崩溃恢复与恢复记录导出的唯一事实来源。

每个事件一行 JSON，带单调递增的 seq 与协调器时钟时间戳。协调器的
每一次状态变迁（命令发出/确认/拒绝/超时/中止、节点就绪/阻塞、控制
模式与阶段切换、人工处置）都先落盘再生效，进程重启后据此重放。
"""

from __future__ import annotations

import json
from pathlib import Path


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.events.append(json.loads(line))
        self._seq = max((e.get("seq", 0) for e in self.events), default=0)
        self._fh = self.path.open("a", encoding="utf-8")

    def append(self, event_type: str, ts: float | None = None, **payload) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "ts": ts, "type": event_type, **payload}
        self.events.append(event)
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()
        return event

    @staticmethod
    def read(path: str | Path) -> list[dict]:
        p = Path(path)
        if not p.exists():
            return []
        return [
            json.loads(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def close(self) -> None:
        self._fh.close()

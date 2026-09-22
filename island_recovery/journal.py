"""事件溯源日志：协调器的全部状态变化先写盘，再对外生效。

日志采用 JSON Lines，每行一个事件。协调器任意时刻的状态都可由日志重放
得到；进程故障后重启时重放日志即可继续，且命令序号单调不复用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class JournalCorrupted(RuntimeError):
    def __init__(self, path: Path, lineno: int, reason: str):
        super().__init__(f"日志损坏 {path}:{lineno}：{reason}")
        self.path = path
        self.lineno = lineno


class Event:
    __slots__ = ("seq", "name", "payload")

    def __init__(self, seq: int, name: str, payload: dict[str, Any]):
        self.seq = seq
        self.name = name
        self.payload = payload

    def to_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "name": self.name, "payload": self.payload},
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, line: str) -> "Event":
        raw = json.loads(line)
        return cls(seq=int(raw["seq"]), name=raw["name"], payload=raw.get("payload", {}))


class Journal:
    """append-only 的 JSONL 事件日志。

    写入使用 O_APPEND + fsync；构造时一次性校验序号连续并缓存末尾序号，
    之后追加只递增内存计数，避免反复全量读盘。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[Event] = list(self._load())
        self._next_seq = (self._events[-1].seq + 1) if self._events else 1

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def _load(self) -> Iterator[Event]:
        if not self.path.exists():
            return iter(())
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = Event.from_json(line)
                except json.JSONDecodeError as exc:
                    raise JournalCorrupted(self.path, lineno, str(exc)) from exc
                if events and event.seq != events[-1].seq + 1:
                    raise JournalCorrupted(
                        self.path,
                        lineno,
                        f"事件序号不连续：期望 {events[-1].seq + 1}，实际 {event.seq}",
                    )
                events.append(event)
        return iter(events)

    def append(self, name: str, payload: dict[str, Any] | None = None) -> Event:
        event = Event(self._next_seq, name, dict(payload or {}))
        line = event.to_json() + "\n"
        # O_APPEND 保证重试/外部追加不覆盖既有内容；fsync 保证崩溃后可重放。
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._events.append(event)
        self._next_seq += 1
        return event

    def read(self) -> list[Event]:
        return self.events


def initialize_journal(path: str | Path, topology_source: Any) -> Path:
    """在新日志写入起始事件（含拓扑快照，重放不依赖外部文件未被改动）。"""
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        raise FileExistsError(f"日志已存在，拒绝覆盖：{path}")
    if isinstance(topology_source, (str, Path)):
        topo_data = json.loads(Path(topology_source).read_text(encoding="utf-8"))
    else:
        topo_data = topology_source
    journal = Journal(path)
    journal.append("recovery.started", {"schema": SCHEMA_VERSION, "topology": topo_data})
    return path

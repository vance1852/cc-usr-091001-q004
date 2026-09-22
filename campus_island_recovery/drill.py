"""故障演练序列执行器：指挥人员逐条注入既有设备响应。

演练 JSON 中的每条响应带 delay_seconds，表示设备在收到命令后多少秒
返回。注入时先把虚拟时钟推进到响应到达时刻并结算超时，再把响应交给
协调器——因此"响应晚于超时"这类情形会被如实地走成"迟到响应"，
而不是被悄悄放行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .clock import VirtualClock
from .coordinator import Coordinator
from .journal import Journal
from .protocol import CommandStatus, Response
from .topology import Topology

#: 已关闭、对应响应视为"已消费"的命令状态
_CLOSED = {
    CommandStatus.CONFIRMED,
    CommandStatus.REJECTED,
    CommandStatus.TIMEOUT,
    CommandStatus.ABORTED,
}


class DrillRunner:
    def __init__(
        self,
        drill: str | Path | dict,
        journal_path: str | Path,
        command_timeout: float = 30.0,
        clock: VirtualClock | None = None,
    ):
        data = self._load(drill)
        self.topology = Topology.from_dict(data)
        self.clock = clock or VirtualClock()
        self.journal = Journal(journal_path)
        self.coordinator = Coordinator(
            self.topology, self.journal, self.clock, command_timeout
        )
        self._responses: list[dict] = list(data.get("responses", []))
        self._cursor = 0

    @classmethod
    def resume(
        cls,
        drill: str | Path | dict,
        journal_path: str | Path,
        command_timeout: float = 30.0,
    ) -> "DrillRunner":
        """从既有 journal 恢复演练：协调器重放事件，响应游标跳过已消费的条目。"""
        data = cls._load(drill)
        topology = Topology.from_dict(data)
        events = Journal.read(journal_path)
        start = max((e.get("ts") or 0.0 for e in events), default=0.0)
        self = cls.__new__(cls)
        self.topology = topology
        self.clock = VirtualClock(start=start)
        self.coordinator = Coordinator.restore(
            topology, str(journal_path), self.clock, command_timeout
        )
        self.journal = self.coordinator.journal
        self._responses = list(data.get("responses", []))
        self._cursor = 0
        # 跳过已在 journal 中关闭的响应；在途转 UNCERTAIN 的仍可补录
        while self._cursor < len(self._responses):
            cid = self._responses[self._cursor]["command_id"]
            cmd = self.coordinator.commands.get(cid)
            if cmd is not None and cmd.status in _CLOSED:
                self._cursor += 1
            else:
                break
        return self

    @staticmethod
    def _load(drill: str | Path | dict) -> dict:
        if isinstance(drill, dict):
            return drill
        return json.loads(Path(drill).read_text(encoding="utf-8"))

    def next_response(self) -> dict | None:
        if self._cursor >= len(self._responses):
            return None
        return self._responses[self._cursor]

    def inject_next(self) -> dict[str, Any]:
        """注入下一条既有响应；返回协调器的处理结果。"""
        resp = self.next_response()
        if resp is None:
            return {"ok": False, "outcome": "exhausted", "detail": "演练响应已全部注入"}
        cid = resp["command_id"]
        cmd = self.coordinator.commands.get(cid)
        if cmd is None:
            return {"ok": False, "outcome": "drill_misaligned",
                    "detail": f"协调器尚未发出 {cid}，无法注入该响应"}
        arrival = cmd.issued_at + float(resp.get("delay_seconds", 0))
        if arrival > self.clock.now():
            self.clock.advance(arrival)
        self.coordinator.tick()  # 先结算超时，迟到的响应才会被如实识别
        result = self.coordinator.inject_response(
            Response(
                command_id=cid,
                device=resp["device"],
                status=resp["status"],
                detail=resp.get("detail", ""),
            )
        )
        self._cursor += 1
        return result

    def run_all(self) -> list[dict[str, Any]]:
        results = []
        while self.next_response() is not None:
            result = self.inject_next()
            results.append(result)
            if result["outcome"] in ("drill_misaligned", "exhausted"):
                break
        return results

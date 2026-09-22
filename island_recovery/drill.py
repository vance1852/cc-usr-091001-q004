"""故障演练播放器：把评审通过的响应脚本逐条喂给协调器。

脚本中的每条响应按 command_id 关联到协调器实际下发的命令，在
“下发时刻 + delay_seconds”到达。响应早于命令、命令已终结等情况都会
走协调器的正常关联检查，从而在演练中暴露乱序/迟到回复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coordinator import RecoveryCoordinator


class SimClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class ScriptedResponse:
    command_id: str
    status: str
    delay: float
    device: str | None = None
    permanent: bool = True
    reason: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScriptedResponse":
        return cls(
            command_id=raw["command_id"],
            status=raw["status"],
            delay=float(raw.get("delay_seconds", 0)),
            device=raw.get("device"),
            permanent=bool(raw.get("permanent", True)),
            reason=str(raw.get("reason", "")),
        )


class DrillPlayer:
    """以固定步长推进模拟时钟，按到期时刻投递脚本响应，再推进协调器。"""

    def __init__(
        self,
        coordinator: RecoveryCoordinator,
        responses: list[ScriptedResponse | dict[str, Any]],
        *,
        clock: SimClock | None = None,
    ):
        self.coord = coordinator
        self.clock = clock or SimClock()
        self.coord.clock = self.clock
        normalized = [
            r if isinstance(r, ScriptedResponse) else ScriptedResponse.from_dict(r)
            for r in responses
        ]
        self.script = {r.command_id: r for r in normalized}
        self.scheduled: dict[str, float] = {}  # command_id -> 到期时刻
        self.trace: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, coordinator: RecoveryCoordinator, path: str | Path) -> "DrillPlayer":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(coordinator, data.get("responses", []))

    def _deliver_due(self) -> list[dict[str, Any]]:
        due = sorted((due, cid) for cid, due in self.scheduled.items() if due <= self.clock.t)
        results = []
        for _, cid in due:
            resp = self.script[cid]
            result = self.coord.inject_response(
                cid, resp.status, device=resp.device,
                permanent=resp.permanent, reason=resp.reason, at=self.clock.t,
            )
            self.scheduled.pop(cid, None)
            results.append({"at": self.clock.t, "response": cid, **result})
            self.trace.append(results[-1])
        return results

    def step(self, dt: float = 1.0) -> dict[str, Any] | None:
        """推进一个时间步；恢复结束（完成/中止/人工停靠）时返回 None。"""
        if self._finished():
            return None
        deliveries = self._deliver_due()
        result = self.coord.tick()
        record = {"at": self.clock.t, "tick": result, "deliveries": deliveries}
        self.trace.append(record)
        if result.get("action") == "issued":
            cid = result["command_id"]
            if cid in self.script:
                self.scheduled[cid] = self.clock.t + self.script[cid].delay
        self.clock.advance(dt)
        return record

    def run(self, dt: float = 1.0, max_seconds: float = 3600) -> list[dict[str, Any]]:
        steps = 0
        max_steps = int(max_seconds / dt) + 1
        while not self._finished() and steps < max_steps:
            self.step(dt)
            steps += 1
        if steps >= max_steps and not self._finished():
            raise TimeoutError("演练在限定时间内未到达结束状态（脚本可能缺少响应）")
        return self.trace

    def _finished(self) -> bool:
        c = self.coord
        return bool(c.terminal) or not c.automation

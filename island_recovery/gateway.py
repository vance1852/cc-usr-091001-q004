"""设备侧网关：协调器与真实设备（或演练模拟器）之间的唯一出口。

协调器严格保证同一时刻只有一条未终结命令，因此网关无需处理并发开合。
所有下发与中止都会被录制，便于把设备往返与恢复记录关联起来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

Verb = str  # "open" | "start" | "close"


@dataclass(frozen=True)
class GatewayCommand:
    command_id: str
    verb: Verb
    device: str


class DeviceGateway(Protocol):
    def send(self, command_id: str, verb: Verb, device: str) -> None: ...

    def abort(self, command_id: str, reason: str) -> None: ...


class RecordingGateway:
    """演练用网关：只录制命令，不接触任何真实设备。

    命令是否到达设备、设备如何回复，由演练指挥通过协调器的
    inject_response / 时钟推进逐条注入。
    """

    def __init__(self) -> None:
        self.sent: list[GatewayCommand] = []
        self.aborted: list[tuple[str, str]] = []

    def send(self, command_id: str, verb: Verb, device: str) -> None:
        self.sent.append(GatewayCommand(command_id, verb, device))

    def abort(self, command_id: str, reason: str) -> None:
        self.aborted.append((command_id, reason))

    def device_commands(self, device: str) -> list[GatewayCommand]:
        return [c for c in self.sent if c.device == device]

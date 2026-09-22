"""设备握手协议：命令生命周期、关联编号与响应。

命令编号由协调系统生成并全程唯一；设备返回的响应必须携带同一编号，
用于识别乱序、迟到与重复响应。编号不复用——即使协调器崩溃重启，
也从 journal 中恢复计数继续递增。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class CommandStatus(str, enum.Enum):
    ISSUED = "issued"        # 已发出，等待设备确认
    CONFIRMED = "confirmed"  # 设备确认执行完成
    REJECTED = "rejected"    # 设备拒绝（互锁/故障），视为永久拒绝直至人工处置
    TIMEOUT = "timeout"      # 超时未收到响应
    ABORTED = "aborted"      # 被协调器中止（受控降级/人工接管/整体中止）
    UNCERTAIN = "uncertain"  # 协调器重启时仍在途，结果未知


#: 已关闭、不再接受响应直接推进的状态
TERMINAL_STATUSES = frozenset(
    {
        CommandStatus.CONFIRMED,
        CommandStatus.REJECTED,
        CommandStatus.TIMEOUT,
        CommandStatus.ABORTED,
    }
)


@dataclass
class Command:
    """一条已发出的设备命令及其生命周期。"""

    command_id: str
    node_id: str
    verb: str
    issued_at: float
    timeout_at: float
    status: CommandStatus = CommandStatus.ISSUED
    supersedes: str | None = None  # 人工重试时被取代的旧命令编号
    closed_at: float | None = None
    detail: str | None = None

    @property
    def in_flight(self) -> bool:
        return self.status == CommandStatus.ISSUED


@dataclass(frozen=True)
class Response:
    """设备返回的响应；command_id 是与命令关联的唯一凭据。"""

    command_id: str
    device: str
    status: str  # "confirmed" | "rejected"
    detail: str = ""

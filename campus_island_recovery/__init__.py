"""园区孤网恢复协调系统。

安全原则：任何自动恢复必须先确认并网点隔离，再取得稳定电源与母线
条件，然后才按负荷优先级送电。执行过程可暂停、可接管、可恢复；
命令从发出到确认、超时、拒绝、中止全程保留关联编号；进程重启后
不再次操作已经确认的开关。
"""

from .topology import COMMAND_VERBS, Node, Topology, TopologyError
from .protocol import Command, CommandStatus, Response
from .journal import Journal
from .coordinator import Control, Coordinator, NodeState, Phase
from .drill import DrillRunner
from .report import build_record, export_record, render_record_text, render_status

__version__ = "0.1.0"

__all__ = [
    "COMMAND_VERBS",
    "Command",
    "CommandStatus",
    "Control",
    "Coordinator",
    "DrillRunner",
    "Journal",
    "Node",
    "NodeState",
    "Phase",
    "Response",
    "Topology",
    "TopologyError",
    "build_record",
    "export_record",
    "render_record_text",
    "render_status",
]

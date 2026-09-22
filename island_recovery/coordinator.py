"""孤网恢复协调器：把安全原则固化为可暂停、可接管、可恢复的状态机。

状态推进只有一个内部入口 ``_tick``，且严格遵守：
并网点隔离确认 → 构网电源稳定 → 母线条件成立 → 负荷按优先级送电。

设备命令的生命周期（issued → confirmed/rejected/timed_out/aborted）全部
带 command_id 落盘；乱序、重复、迟到或无法关联的响应只记录，绝不推进状态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .gateway import DeviceGateway
from .journal import Journal
from .topology import Topology, load_topology

# 各类设备命令的默认确认时限（秒），可在构造时覆盖。
DEFAULT_TIMEOUTS = {"isolation": 10, "source": 60, "load": 15}
DEFAULT_MAX_ATTEMPTS = 2  # 首发 + 一次超时/暂态拒绝后的重发


class RecoveryError(RuntimeError):
    """请求的动作在当前状态下不被安全规则允许。"""


class Clock:
    def now(self) -> float:
        return time.time()


@dataclass
class CommandRecord:
    command_id: str
    verb: str
    node: str
    issued_at: float
    deadline: float
    attempt: int
    status: str = "pending"  # pending|confirmed|rejected|timed_out|aborted
    permanent: bool = False
    reason: str = ""
    closed_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.status != "pending"


@dataclass
class _NodeState:
    status: str = "pending"  # pending|confirmed|rejected
    manually_confirmed: bool = False
    reason: str = ""


class RecoveryCoordinator:
    def __init__(
        self,
        journal: Journal,
        topology: Topology,
        gateway: DeviceGateway,
        *,
        clock: Clock | None = None,
        timeouts: dict[str, int] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.journal = journal
        self.topology = topology
        self.gateway = gateway
        self.clock = clock or Clock()
        self.timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self.max_attempts = max_attempts
        self._on_event = on_event

        self.plan = topology.plan()
        self.node_ids = [n.id for n in self.plan]
        self.nodes = {n.id: _NodeState() for n in self.plan}
        self.commands: dict[str, CommandRecord] = {}
        self.active_id: str | None = None
        self.attempts: dict[str, int] = {n.id: 0 for n in self.plan}

        self.mode = "running"          # running|paused
        self.automation = True         # False 表示人工接管
        self.grid = "absent"           # absent|returned
        self.terminal: str | None = None
        self.terminal_reason = ""
        # 人工处置停靠点（非终态）：现场处置并确认后可继续恢复。
        self.hold_node: str | None = None
        self.hold_reason = ""

        self._replay()

    # ------------------------------------------------------------------ 构造

    @classmethod
    def start(
        cls,
        journal_path: str | Path,
        topology: Topology | dict[str, Any] | str | Path,
        gateway: DeviceGateway,
        **kwargs: Any,
    ) -> "RecoveryCoordinator":
        """建立新日志并启动一次恢复。拓扑会快照进日志。"""
        from .journal import initialize_journal

        if isinstance(topology, Topology):
            topo = topology
            topo_data: Any = _topology_snapshot(topo)
        else:
            topo_data = topology
            topo = load_topology(topology)
        initialize_journal(journal_path, topo_data)
        return cls(Journal(journal_path), topo, gateway, **kwargs)

    @classmethod
    def recover(
        cls,
        journal_path: str | Path,
        gateway: DeviceGateway,
        **kwargs: Any,
    ) -> "RecoveryCoordinator":
        """从日志重放恢复。

        重放不会重新下发任何命令：崩溃前已确认的开关保持确认，崩溃前
        在途的命令保持在途（设备迟到确认仍可关联入账）。
        """
        journal = Journal(journal_path)
        events = journal.read()
        if not events or events[0].name != "recovery.started":
            raise RecoveryError("日志缺少 recovery.started，无法恢复")
        topo = load_topology(events[0].payload["topology"])
        return cls(journal, topo, gateway, **kwargs)

    # ------------------------------------------------------------- 事件落盘

    def _emit(self, name: str, payload: dict[str, Any] | None = None) -> None:
        event = self.journal.append(name, payload)
        self._apply(event.name, event.payload)
        if self._on_event:
            self._on_event(event.name, event.payload)

    def _replay(self) -> None:
        for event in self.journal.read():
            if event.name == "recovery.started":
                continue
            self._apply(event.name, event.payload)

    def _apply(self, name: str, p: dict[str, Any]) -> None:
        """纯状态归并：同一段日志重放任意次结果一致。"""
        if name == "recovery.paused":
            self.mode = "paused"
        elif name == "recovery.resumed":
            self.mode = "running"
        elif name == "automation.suspended":
            self.automation = False
        elif name == "automation.resumed":
            self.automation = True
        elif name == "grid.returned":
            self.grid = "returned"
        elif name == "node.confirmed":
            self.nodes[p["node"]].status = "confirmed"
        elif name == "manual.node_confirmed":
            state = self.nodes[p["node"]]
            state.status = "confirmed"
            state.manually_confirmed = True
        elif name == "command.issued":
            rec = CommandRecord(
                command_id=p["command_id"],
                verb=p["verb"],
                node=p["node"],
                issued_at=p["issued_at"],
                deadline=p["deadline"],
                attempt=p["attempt"],
            )
            self.commands[rec.command_id] = rec
            self.active_id = rec.command_id
            self.attempts[rec.node] = rec.attempt
        elif name in ("command.confirmed", "command.rejected",
                      "command.timed_out", "command.aborted"):
            rec = self.commands[p["command_id"]]
            rec.status = name.split(".")[1]
            rec.closed_at = p.get("at")
            rec.reason = p.get("reason", "")
            rec.permanent = bool(p.get("permanent", False))
            if self.active_id == rec.command_id:
                self.active_id = None
            if name == "command.confirmed":
                self.nodes[rec.node].status = "confirmed"
            elif name == "command.rejected" and rec.permanent:
                state = self.nodes[rec.node]
                state.status = "rejected"
                state.reason = rec.reason or "设备永久拒绝"
        elif name == "recovery.completed":
            self.terminal = "completed"
            self.terminal_reason = p.get("reason", "全部节点恢复")
        elif name == "recovery.aborted":
            self.terminal = "aborted"
            self.terminal_reason = p.get("reason", "")
        elif name == "manual.hold":
            self.hold_node = p["node"]
            self.hold_reason = p.get("reason", "")
        elif name == "manual.hold_cleared":
            self.hold_node = None
            self.hold_reason = ""
        # response.duplicate / response.late / response.uncorrelated：
        # 只保留在日志里，不改变任何状态。

    # ------------------------------------------------------------- 主推进

    def _enter_hold(self, node_id: str, reason: str, now: float) -> dict[str, Any]:
        """进入人工处置停靠点：自动挂起自动化并记录停靠原因。"""
        self._emit("automation.suspended", {
            "reason": f"节点 {node_id} 需要人工处置", "operator": "system", "at": now,
        })
        self._emit("manual.hold", {"node": node_id, "reason": reason, "at": now})
        return {"action": "manual_hold", "node": node_id, "reason": reason}

    def tick(self, now: float | None = None) -> dict[str, Any]:
        """推进一个自动决策步。返回本步采取的动作说明（空动作也安全）。

        暂停、人工接管、终态或存在在途命令时不自动下发。每步至多处理
        一个节点，保证命令之间有明确的确认边界。
        """
        now = self.clock.now() if now is None else now
        if self.terminal:
            return {"action": "none", "reason": f"终态：{self.terminal}（{self.terminal_reason}）"}

        # 市电提前返回优先处理：即使处于暂停状态也立即受控降级，
        # 中止在途命令后交人工执行市电同期/回切。
        if self.grid == "returned":
            return self._handle_grid_return(now)

        if self.mode == "paused":
            return {"action": "none", "reason": "已暂停，等待指挥人员继续"}
        if not self.automation:
            return {"action": "none", "reason": "自动化已挂起，处于人工接管模式"}

        # 在途命令：只检查超时，绝不叠加新命令。
        if self.active_id:
            rec = self.commands[self.active_id]
            if now >= rec.deadline:
                self.gateway.abort(rec.command_id, "confirmation-timeout")
                self._emit("command.timed_out", {
                    "command_id": rec.command_id, "node": rec.node,
                    "verb": rec.verb, "attempt": rec.attempt, "at": now,
                    "deadline": rec.deadline,
                })
                return {"action": "timed_out", "command_id": rec.command_id, "node": rec.node}
            return {"action": "awaiting_confirmation", "command_id": rec.command_id,
                    "node": rec.node, "deadline": rec.deadline, "remaining": rec.deadline - now}

        return self._advance_next_node(now)

    def _advance_next_node(self, now: float) -> dict[str, Any]:
        for node in self.plan:
            state = self.nodes[node.id]
            if state.status == "confirmed":
                continue
            if state.status == "rejected":
                return self._enter_hold(node.id, state.reason, now)

            unmet = [r for r in node.requires if self.nodes[r].status != "confirmed"]
            if unmet:
                # 拓扑排序下不应发生；作为最后一道门禁拦截。
                return {"action": "blocked", "node": node.id,
                        "reason": f"前置条件未确认：{unmet}"}

            if not node.actionable:
                # 母线无可操作开关，上游稳定即视为带电条件成立。
                self._emit("node.confirmed", {
                    "node": node.id, "at": now,
                    "reason": "母线随上游电源稳定而带电，无开关命令",
                })
                return {"action": "bus_condition_met", "node": node.id}

            if self.attempts[node.id] >= self.max_attempts:
                return self._enter_hold(
                    node.id,
                    f"节点 {node.id} 已尝试 {self.attempts[node.id]} 次仍未确认，转人工处置",
                    now,
                )

            attempt = self.attempts[node.id] + 1
            number = 1 + sum(1 for c in self.commands.values())
            command_id = f"{node.command_verb}-{number:02d}"
            deadline = now + self.timeouts[node.kind]
            # 先落盘再出口：崩溃发生在落盘之后只会让命令保持在途，
            # 绝不会因重启而对同一开关重复操作。
            self._emit("command.issued", {
                "command_id": command_id, "verb": node.command_verb,
                "node": node.id, "attempt": attempt,
                "issued_at": now, "deadline": deadline,
            })
            self.gateway.send(command_id, node.command_verb, node.id)
            return {"action": "issued", "command_id": command_id,
                    "verb": node.command_verb, "node": node.id, "deadline": deadline}

        self._emit("recovery.completed", {"at": now})
        return {"action": "completed"}

    def _handle_grid_return(self, now: float) -> dict[str, Any]:
        if self.active_id:
            rec = self.commands[self.active_id]
            self.gateway.abort(rec.command_id, "grid-returned-controlled-derating")
            self._emit("command.aborted", {
                "command_id": rec.command_id, "node": rec.node, "verb": rec.verb,
                "reason": "市电提前返回，受控降级", "at": now,
            })
        self._emit("recovery.aborted", {
            "reason": "市电提前返回：孤网恢复中止，转入市电同期/回切程序，等待人工确认",
            "at": now,
        })
        return {"action": "derated", "reason": self.terminal_reason}

    # ----------------------------------------------------- 设备响应注入

    def inject_response(
        self,
        command_id: str,
        status: str,
        *,
        device: str | None = None,
        permanent: bool = True,
        reason: str = "",
        at: float | None = None,
    ) -> dict[str, Any]:
        """注入一条设备响应（演练逐条注入，或现场网关回调）。

        只有“当前在途且节点尚未确认”的命令的有效确认/拒绝能推进状态；
        未知编号、重复确认、乱序/迟到回复一律只入账，不误推进。
        """
        now = self.clock.now() if at is None else at
        rec = self.commands.get(command_id)
        if rec is None:
            self._emit("response.uncorrelated", {
                "command_id": command_id, "status": status,
                "device": device, "at": now,
                "reason": "找不到该命令编号，可能为迟到回复或伪造报文",
            })
            return {"accepted": False, "disposition": "uncorrelated"}

        if device is not None and device != rec.node:
            self._emit("response.uncorrelated", {
                "command_id": command_id, "status": status, "device": device,
                "expected_device": rec.node, "at": now,
                "reason": "响应设备与命令目标不一致",
            })
            return {"accepted": False, "disposition": "device_mismatch"}

        if rec.terminal:
            kind = "duplicate" if rec.status == status else "late"
            self._emit(f"response.{kind}", {
                "command_id": command_id, "status": status,
                "recorded_status": rec.status, "at": now,
                "node": rec.node,
                "reason": "命令已终结，重复/乱序回复不改变状态",
            })
            return {"accepted": False, "disposition": kind, "recorded_status": rec.status}

        if self.nodes[rec.node].status == "confirmed":
            self._emit("response.late", {
                "command_id": command_id, "status": status, "at": now,
                "node": rec.node, "reason": "节点已确认，迟到回复不重复操作",
            })
            return {"accepted": False, "disposition": "late"}

        if status == "confirmed":
            self._emit("command.confirmed", {
                "command_id": command_id, "node": rec.node,
                "verb": rec.verb, "attempt": rec.attempt, "at": now,
            })
            return {"accepted": True, "disposition": "confirmed", "node": rec.node}

        if status == "rejected":
            self._emit("command.rejected", {
                "command_id": command_id, "node": rec.node,
                "verb": rec.verb, "attempt": rec.attempt,
                "permanent": permanent, "reason": reason, "at": now,
            })
            if permanent:
                hold = self._enter_hold(
                    rec.node,
                    f"设备 {rec.node} 永久拒绝命令 {command_id}：{reason or '未说明'}",
                    now,
                )
                return {"accepted": True, "disposition": "permanent_reject",
                        "node": rec.node, **hold}
            return {"accepted": True, "disposition": "transient_reject", "node": rec.node}

        self._emit("response.uncorrelated", {
            "command_id": command_id, "status": status, "at": now,
            "reason": f"无法识别的响应状态：{status}",
        })
        return {"accepted": False, "disposition": "invalid_status"}

    # ------------------------------------------------------------- 人工动作

    def pause(self, reason: str, operator: str = "operator") -> None:
        if self.terminal:
            raise RecoveryError("恢复已终结，无需暂停")
        if self.mode == "paused":
            raise RecoveryError("当前已处于暂停状态")
        self._emit("recovery.paused", {"reason": reason, "operator": operator,
                                       "at": self.clock.now()})

    def resume(self, operator: str = "operator") -> None:
        if self.terminal:
            raise RecoveryError("恢复已终结，不能继续")
        if self.mode != "paused":
            raise RecoveryError("当前未暂停")
        self._emit("recovery.resumed", {"operator": operator, "at": self.clock.now()})

    def suspend_automation(self, reason: str, operator: str = "operator") -> None:
        """人工接管：自动化停止发令，但协调器继续记录与解释状态。"""
        if self.terminal:
            raise RecoveryError("恢复已终结")
        if not self.automation:
            raise RecoveryError("已处于人工接管模式")
        self._emit("automation.suspended", {"reason": reason, "operator": operator,
                                            "at": self.clock.now()})

    def resume_automation(self, operator: str = "operator") -> None:
        if self.terminal:
            raise RecoveryError("恢复已终结，不能恢复自动化")
        if self.automation:
            raise RecoveryError("自动化未挂起")
        if self.hold_node and self.nodes[self.hold_node].status != "confirmed":
            raise RecoveryError(
                f"人工处置停靠点 {self.hold_node} 尚未解除：请先现场核实并人工确认该节点"
            )
        self._emit("automation.resumed", {"operator": operator, "at": self.clock.now()})

    def manual_confirm(self, node_id: str, operator: str, note: str = "") -> None:
        """人工现场确认某节点已到位（如就地合环/手动启机后的补录）。

        拓扑前置条件仍然强制：上游未确认时不允许人工跳过安全顺序。
        在途命令未终结前也不允许确认，避免与设备状态打架。
        """
        if self.automation and self.mode != "paused":
            raise RecoveryError("请先暂停或挂起自动化，再进行人工处置")
        node = self.topology.get(node_id)
        if node_id not in self.nodes:
            raise RecoveryError(
                f"节点 {node_id} 不在自动恢复计划内（如柴油发电机仅能走人工程序）"
            )
        state = self.nodes[node_id]
        if self.terminal:
            raise RecoveryError(f"恢复已终结（{self.terminal}），不能再确认节点")
        if state.status == "confirmed":
            raise RecoveryError(f"节点 {node_id} 已确认，禁止重复操作")
        unmet = [r for r in node.requires if self.nodes[r].status != "confirmed"]
        if unmet:
            raise RecoveryError(f"节点 {node_id} 的前置条件未确认：{unmet}，拓扑顺序不可绕过")
        active = self._command_for_node(node_id)
        if active and not active.terminal:
            raise RecoveryError(f"命令 {active.command_id} 尚在途，请先等待或中止它")
        self._emit("manual.node_confirmed", {
            "node": node_id, "operator": operator, "note": note,
            "at": self.clock.now(),
        })
        if self.hold_node == node_id:
            self._emit("manual.hold_cleared", {
                "node": node_id, "operator": operator, "at": self.clock.now(),
                "reason": "现场处置完成，节点已人工确认",
            })

    def abort_active_command(self, reason: str, operator: str = "operator") -> None:
        """中止在途命令。中止后节点保持未确认，由人工决定后续。"""
        if self.automation and self.mode != "paused":
            raise RecoveryError("请先暂停或挂起自动化，再中止在途命令")
        if not self.active_id:
            raise RecoveryError("当前没有在途命令")
        rec = self.commands[self.active_id]
        self.gateway.abort(rec.command_id, f"manual-abort: {reason}")
        self._emit("command.aborted", {
            "command_id": rec.command_id, "node": rec.node, "verb": rec.verb,
            "reason": f"人工中止：{reason}", "operator": operator, "at": self.clock.now(),
        })

    def grid_returned(self, at: float | None = None) -> None:
        """市电提前返回信号。下一推进步进入受控降级。"""
        if self.terminal:
            raise RecoveryError("恢复已终结")
        if self.grid == "returned":
            raise RecoveryError("已记录市电返回")
        self._emit("grid.returned", {"at": self.clock.now() if at is None else at})

    def manual_abort(self, reason: str, operator: str = "operator") -> None:
        if self.terminal:
            raise RecoveryError("恢复已终结")
        if self.active_id:
            rec = self.commands[self.active_id]
            self.gateway.abort(rec.command_id, f"manual-abort-recovery: {reason}")
            self._emit("command.aborted", {
                "command_id": rec.command_id, "node": rec.node, "verb": rec.verb,
                "reason": f"人工终止恢复：{reason}", "operator": operator,
                "at": self.clock.now(),
            })
        self._emit("recovery.aborted", {
            "reason": f"人工终止：{reason}", "operator": operator,
            "at": self.clock.now(),
        })

    # ------------------------------------------------------------- 态势解释

    def _command_for_node(self, node_id: str) -> CommandRecord | None:
        for rec in self.commands.values():
            if rec.node == node_id and not rec.terminal:
                return rec
        return None

    def node_view(self, node_id: str) -> dict[str, Any]:
        node = self.topology.get(node_id)
        state = self.nodes[node_id]
        unmet = [r for r in node.requires if self.nodes[r].status != "confirmed"]
        active = self._command_for_node(node_id)
        if state.status == "confirmed":
            disposition = "已确认（人工补录）" if state.manually_confirmed else "已确认"
        elif self.hold_node == node_id:
            disposition = f"等待人工处置：{self.hold_reason}"
        elif state.status == "rejected":
            disposition = f"永久拒绝，等待人工处置：{state.reason}"
        elif active:
            disposition = f"命令 {active.command_id} 在途，等待设备确认"
        elif unmet:
            names = "、".join(unmet)
            disposition = f"等待上游：{names}"
        elif self.terminal:
            disposition = f"未动作（恢复已终结：{self.terminal_reason}）"
        else:
            disposition = "条件已满足，等待协调器下发"
        return {
            "node": node_id, "kind": node.kind, "priority": node.priority,
            "status": state.status, "manually_confirmed": state.manually_confirmed,
            "requires": list(node.requires), "unmet_requires": unmet,
            "active_command": active.command_id if active else None,
            "disposition": disposition,
        }

    def allowed_actions(self) -> list[str]:
        """列出此刻指挥人员可采取的动作（演练菜单与安全护栏同源）。"""
        if self.terminal == "completed":
            return ["export_record（导出恢复记录）"]
        if self.terminal:
            return ["export_record（导出恢复记录）", "manual_abort（终止恢复）"]

        actions: list[str] = []
        if self.hold_node:
            nid = self.hold_node
            actions.append(f"manual_confirm(node={nid})（现场核实后人工确认，解除停靠）")
            actions.append("manual_abort（终止恢复）")
            actions.append("export_record（导出恢复记录）")
            return actions

        if self.mode == "running":
            actions.append("pause（暂停自动推进）")
        else:
            actions.append("resume（继续自动推进）")
        if self.automation:
            actions.append("suspend_automation（人工接管）")
        else:
            actions.append("resume_automation（归还自动化）")

        if self.active_id:
            rec = self.commands[self.active_id]
            actions.append(
                f"inject_response(command_id={rec.command_id}, status=confirmed|rejected)")
            if self.mode == "paused" or not self.automation:
                actions.append("abort_active_command（中止在途命令）")
        else:
            actions.append("tick（推进下一步）")

        actions.append("grid_returned（报告市电提前返回，触发受控降级）")
        actions.append("manual_abort（终止恢复）")
        actions.append("export_record（导出恢复记录）")
        return actions

    def status(self) -> dict[str, Any]:
        current = None
        if not self.terminal:
            for nid in self.node_ids:
                if self.nodes[nid].status != "confirmed":
                    current = nid
                    break
        return {
            "mode": self.mode,
            "automation": "自动" if self.automation else "人工接管",
            "grid": self.grid,
            "terminal": self.terminal,
            "terminal_reason": self.terminal_reason,
            "active_command": self.active_id,
            "hold_node": self.hold_node,
            "hold_reason": self.hold_reason,
            "current_node": current,
            "plan": self.node_ids,
            "nodes": [self.node_view(nid) for nid in self.node_ids],
            "allowed_actions": self.allowed_actions(),
        }

    def explain(self) -> str:
        """生成给指挥人员看的中文态势文本。"""
        s = self.status()
        phase = {
            None: "进行中",
            "completed": "恢复完成",
            "aborted": "已中止（受控降级）",
        }[s["terminal"]]
        lines = [
            f"态势：{phase}｜模式：{s['mode']}｜自动化：{s['automation']}｜市电：{s['grid']}",
        ]
        if s["hold_node"]:
            lines.append(f"人工处置停靠点：{s['hold_node']}（{s['hold_reason']}）")
        if s["active_command"]:
            lines.append(f"在途命令：{s['active_command']}（等待设备确认，乱序回复不会推进）")
        lines.append("节点状态（恢复顺序）：")
        for view in s["nodes"]:
            mark = {"isolation": "隔离开关", "source": "构网电源",
                    "bus": "母线", "load": "负荷"}[view["kind"]]
            lines.append(f"  - [{mark}] {view['node']}：{view['disposition']}")
        lines.append("此刻允许的动作：")
        lines.extend(f"  · {a}" for a in s["allowed_actions"])
        return "\n".join(lines)

    # ------------------------------------------------------------- 记录导出

    def export_record(self) -> dict[str, Any]:
        """导出包含自动决策与人工接管的完整恢复记录。"""
        timeline = []
        for event in self.journal.read():
            operator = event.payload.get("operator")
            actor = "manual" if operator and operator != "system" else "auto"
            timeline.append({
                "seq": event.seq, "at": event.payload.get("at"),
                "actor": actor, "event": event.name, "payload": event.payload,
            })
        commands = [
            {
                "command_id": rec.command_id, "verb": rec.verb, "node": rec.node,
                "attempt": rec.attempt, "issued_at": rec.issued_at,
                "deadline": rec.deadline, "status": rec.status,
                "permanent": rec.permanent, "reason": rec.reason,
                "closed_at": rec.closed_at,
            }
            for rec in self.commands.values()
        ]
        return {
            "outcome": self.terminal or "in_progress",
            "outcome_reason": self.terminal_reason,
            "plan": self.node_ids,
            "final_nodes": [self.node_view(nid) for nid in self.node_ids],
            "commands": commands,
            "timeline": timeline,
            "auto_decisions": sum(1 for t in timeline if t["actor"] == "auto"),
            "manual_interventions": sum(1 for t in timeline if t["actor"] == "manual"),
        }

    def export_record_json(self, path: str | Path) -> Path:
        import json

        path = Path(path)
        path.write_text(
            json.dumps(self.export_record(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path


def _topology_snapshot(topo: Topology) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": n.id, "kind": n.kind, "requires": list(n.requires),
                "priority": n.priority,
                **({"description": n.description} if n.description else {}),
            }
            for n in topo.plan()
        ]
    }

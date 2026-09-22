"""孤网恢复协调器。

把评审通过的安全原则落实为状态机：

1. 并网点隔离确认之前，不启动任何电源；
2. 电源稳定、母线带电之前，不送任何负荷；
3. 负荷严格按优先级依次送电。

命令从发出到确认/超时/拒绝/中止全程保留关联编号；乱序、迟到、
重复的响应只入账，不误推进下一步。设备永久拒绝时停在允许人工
处置的位置；市电提前返回时进入受控降级。进程崩溃后从 journal
恢复：已确认的开关绝不再次操作，在途命令转为"结果未知"并等待
人工补录响应或显式重试。
"""

from __future__ import annotations

import enum
from typing import Any

from .journal import Journal
from .protocol import Command, CommandStatus, Response
from .topology import COMMAND_VERBS, Node, Topology


class Control(str, enum.Enum):
    AUTO = "auto"      # 自动按拓扑前置条件推进
    PAUSED = "paused"  # 暂停下发新命令，在途命令仍被跟踪
    MANUAL = "manual"  # 人工接管，只执行操作员显式动作


class Phase(str, enum.Enum):
    RUNNING = "running"
    BLOCKED = "blocked"    # 拒绝/超时/在途不明：停在允许人工处置的位置
    DEGRADED = "degraded"  # 市电提前返回后的受控降级
    COMPLETED = "completed"
    ABORTED = "aborted"


class NodeState(str, enum.Enum):
    PENDING = "pending"      # 等待前置条件或调度
    COMMANDED = "commanded"  # 命令在途
    READY = "ready"          # 已就绪（设备确认或人工现场确认）
    BLOCKED = "blocked"      # 设备永久拒绝
    UNCERTAIN = "uncertain"  # 超时或重启时在途，现场状态未知


TERMINAL_PHASES = frozenset({Phase.COMPLETED, Phase.ABORTED})


class Coordinator:
    """孤网恢复协调器。

    所有公开操作返回 dict 结果（至少含 ok / outcome / detail），
    便于演练 CLI 直接展示，也便于测试断言。
    """

    def __init__(
        self,
        topology: Topology,
        journal: Journal,
        clock,
        command_timeout: float = 30.0,
        _announce: bool = True,
    ):
        self.topology = topology
        self.journal = journal
        self.clock = clock
        self.command_timeout = float(command_timeout)
        self.control = Control.AUTO
        self.phase = Phase.RUNNING
        self.node_state: dict[str, NodeState] = {n.id: NodeState.PENDING for n in topology}
        self.node_ready_via: dict[str, str] = {}
        self.commands: dict[str, Command] = {}
        self.node_command: dict[str, str] = {}  # 节点 -> 当前在途命令编号
        self._seq = 0
        if _announce:
            self._emit(
                "coordinator_started",
                nodes=[{"id": n.id, "kind": n.kind, "requires": list(n.requires)} for n in topology],
                command_timeout=self.command_timeout,
            )
            self._passive_update()
            self._evaluate()

    # ------------------------------------------------------------------
    # 崩溃恢复
    # ------------------------------------------------------------------
    @classmethod
    def restore(
        cls,
        topology: Topology,
        journal_path: str,
        clock,
        command_timeout: float = 30.0,
    ) -> "Coordinator":
        """从既有 journal 恢复协调器。

        已确认的节点保持就绪，绝不重新下令；崩溃时仍在途的命令转为
        UNCERTAIN 并阻塞自动推进，等待人工补录响应或显式重试。
        """
        journal = Journal(journal_path)
        coord = cls(topology, journal, clock, command_timeout, _announce=False)
        coord._replay(list(journal.events))
        for cmd in coord.commands.values():
            if cmd.status == CommandStatus.ISSUED:
                cmd.status = CommandStatus.UNCERTAIN
                cmd.detail = "协调器重启时在途"
                coord.node_state[cmd.node_id] = NodeState.UNCERTAIN
                coord._emit("command_uncertain", command_id=cmd.command_id, node_id=cmd.node_id)
                coord._emit("node_uncertain", node_id=cmd.node_id, reason="协调器重启时在途")
        coord._emit(
            "coordinator_restored",
            commands=len(coord.commands),
            phase=coord.phase.value,
        )
        coord._refresh_phase()
        coord._evaluate()
        return coord

    def _replay(self, events: list[dict]) -> None:
        for ev in events:
            etype = ev["type"]
            if etype == "command_issued":
                cmd = Command(
                    command_id=ev["command_id"],
                    node_id=ev["node_id"],
                    verb=ev["verb"],
                    issued_at=ev.get("ts") or 0.0,
                    timeout_at=ev.get("timeout_at", 0.0),
                    supersedes=ev.get("supersedes"),
                )
                self.commands[cmd.command_id] = cmd
                self.node_command[cmd.node_id] = cmd.command_id
                self.node_state[cmd.node_id] = NodeState.COMMANDED
                self._seq = max(self._seq, int(ev.get("command_seq", 0)))
            elif etype == "command_confirmed":
                self._close_command(ev["command_id"], CommandStatus.CONFIRMED, ev)
            elif etype == "command_rejected":
                self._close_command(ev["command_id"], CommandStatus.REJECTED, ev)
            elif etype == "command_timeout":
                self._close_command(ev["command_id"], CommandStatus.TIMEOUT, ev)
            elif etype == "command_aborted":
                self._close_command(ev["command_id"], CommandStatus.ABORTED, ev)
            elif etype == "node_ready":
                self.node_state[ev["node_id"]] = NodeState.READY
                self.node_ready_via[ev["node_id"]] = ev.get("via", "auto")
            elif etype == "node_blocked":
                self.node_state[ev["node_id"]] = NodeState.BLOCKED
            elif etype == "node_uncertain":
                self.node_state[ev["node_id"]] = NodeState.UNCERTAIN
            elif etype == "control_changed":
                self.control = Control(ev["control"])
            elif etype == "phase_changed":
                self.phase = Phase(ev["phase"])
            elif etype == "completed":
                self.phase = Phase.COMPLETED
            elif etype == "aborted":
                self.phase = Phase.ABORTED

    def _close_command(self, command_id: str, status: CommandStatus, ev: dict) -> None:
        cmd = self.commands.get(command_id)
        if cmd is None:
            return
        cmd.status = status
        cmd.closed_at = ev.get("ts")
        cmd.detail = ev.get("detail") or cmd.detail

    # ------------------------------------------------------------------
    # 内部推进逻辑
    # ------------------------------------------------------------------
    def _emit(self, event_type: str, **payload) -> dict:
        return self.journal.append(event_type, ts=self.clock.now(), **payload)

    def _set_phase(self, phase: Phase, reason: str) -> None:
        if self.phase == phase:
            return
        self.phase = phase
        self._emit("phase_changed", phase=phase.value, reason=reason)

    def _refresh_phase(self) -> None:
        """根据节点状态在 RUNNING / BLOCKED 之间切换。"""
        if self.phase in TERMINAL_PHASES or self.phase == Phase.DEGRADED:
            return
        stuck = any(
            state in (NodeState.BLOCKED, NodeState.UNCERTAIN)
            for state in self.node_state.values()
        )
        if stuck and self.phase == Phase.RUNNING:
            self._set_phase(Phase.BLOCKED, "存在被拒绝或结果未知的设备，等待人工处置")
        elif not stuck and self.phase == Phase.BLOCKED:
            self._set_phase(Phase.RUNNING, "阻塞已解除")
            self._evaluate()

    def _passive_update(self) -> None:
        """无源节点（母线）在上游全部就绪时自动视为就绪。"""
        changed = True
        while changed:
            changed = False
            for node in self.topology:
                if node.actionable or self.node_state[node.id] != NodeState.PENDING:
                    continue
                if all(self.node_state[req] == NodeState.READY for req in node.requires):
                    self.node_state[node.id] = NodeState.READY
                    self.node_ready_via[node.id] = "passive"
                    self._emit("node_ready", node_id=node.id, via="passive")
                    changed = True

    def _evaluate(self) -> None:
        """自动推进：满足拓扑前置条件时按优先级下达命令。"""
        if self.phase in TERMINAL_PHASES or self.phase == Phase.DEGRADED:
            return
        self._passive_update()
        if all(state == NodeState.READY for state in self.node_state.values()):
            self._set_phase(Phase.COMPLETED, "全部节点就绪，孤网恢复完成")
            self._emit("completed")
            return
        if self.control != Control.AUTO or self.phase != Phase.RUNNING:
            return
        for node in self.topology.candidates(
            is_ready=lambda nid: self.node_state[nid] == NodeState.READY,
            is_pending=lambda nid: self.node_state[nid] == NodeState.PENDING,
        ):
            self._issue(node)

    def _issue(self, node: Node, supersedes: str | None = None) -> Command:
        self._seq += 1
        verb = COMMAND_VERBS[node.kind]
        command_id = f"{verb}-{self._seq:02d}"
        now = self.clock.now()
        cmd = Command(
            command_id=command_id,
            node_id=node.id,
            verb=verb,
            issued_at=now,
            timeout_at=now + self.command_timeout,
            supersedes=supersedes,
        )
        self.commands[command_id] = cmd
        self.node_command[node.id] = command_id
        self.node_state[node.id] = NodeState.COMMANDED
        self._emit(
            "command_issued",
            command_id=command_id,
            node_id=node.id,
            verb=verb,
            command_seq=self._seq,
            timeout_at=cmd.timeout_at,
            supersedes=supersedes,
        )
        return cmd

    def _mark_ready(self, node_id: str, via: str) -> None:
        self.node_state[node_id] = NodeState.READY
        self.node_ready_via[node_id] = via
        self._emit("node_ready", node_id=node_id, via=via)

    def _abort_in_flight(self, reason: str) -> None:
        for cmd in self.commands.values():
            if cmd.status == CommandStatus.ISSUED:
                cmd.status = CommandStatus.ABORTED
                cmd.closed_at = self.clock.now()
                cmd.detail = reason
                self._emit(
                    "command_aborted",
                    command_id=cmd.command_id,
                    node_id=cmd.node_id,
                    reason=reason,
                )
                if self.node_state[cmd.node_id] == NodeState.COMMANDED:
                    self.node_state[cmd.node_id] = NodeState.UNCERTAIN
                    self._emit("node_uncertain", node_id=cmd.node_id, reason=reason)

    # ------------------------------------------------------------------
    # 设备响应注入（含乱序/迟到/重复识别）
    # ------------------------------------------------------------------
    def inject_response(self, response: Response) -> dict[str, Any]:
        cmd = self.commands.get(response.command_id)
        if cmd is None:
            self._emit(
                "response_orphan",
                command_id=response.command_id,
                device=response.device,
                status=response.status,
            )
            return {"ok": False, "outcome": "orphan",
                    "detail": f"未知关联编号 {response.command_id}，已入账但不推进"}
        if cmd.node_id != response.device:
            self._emit(
                "response_mismatch",
                command_id=cmd.command_id,
                expected=cmd.node_id,
                got=response.device,
            )
            return {"ok": False, "outcome": "mismatch",
                    "detail": f"响应设备 {response.device} 与命令对象 {cmd.node_id} 不符"}
        if cmd.status == CommandStatus.ISSUED:
            return self._close_with_response(cmd, response, resolved=False)
        if cmd.status == CommandStatus.UNCERTAIN:
            # 崩溃恢复后补录的响应：用于解除"结果未知"
            return self._close_with_response(cmd, response, resolved=True)
        duplicate = (
            (cmd.status == CommandStatus.CONFIRMED and response.status == "confirmed")
            or (cmd.status == CommandStatus.REJECTED and response.status == "rejected")
        )
        kind = "duplicate" if duplicate else "late"
        self._emit(
            f"response_{kind}",
            command_id=cmd.command_id,
            node_id=cmd.node_id,
            command_status=cmd.status.value,
            response_status=response.status,
        )
        return {"ok": False, "outcome": kind,
                "detail": f"命令 {cmd.command_id} 已关闭（{cmd.status.value}），响应只入账不推进"}

    def _close_with_response(self, cmd: Command, response: Response, resolved: bool) -> dict[str, Any]:
        now = self.clock.now()
        if response.status == "confirmed":
            cmd.status = CommandStatus.CONFIRMED
            cmd.closed_at = now
            self._emit(
                "command_confirmed",
                command_id=cmd.command_id,
                node_id=cmd.node_id,
                resolved_after_restart=resolved,
            )
            self._mark_ready(cmd.node_id, via="auto")
            self._refresh_phase()
            self._evaluate()
            return {"ok": True, "outcome": "resolved" if resolved else "confirmed",
                    "detail": f"{cmd.node_id} 已确认就绪"}
        if response.status == "rejected":
            cmd.status = CommandStatus.REJECTED
            cmd.closed_at = now
            cmd.detail = response.detail or None
            self._emit(
                "command_rejected",
                command_id=cmd.command_id,
                node_id=cmd.node_id,
                detail=response.detail,
                resolved_after_restart=resolved,
            )
            self.node_state[cmd.node_id] = NodeState.BLOCKED
            self._emit("node_blocked", node_id=cmd.node_id, reason=f"设备拒绝 {cmd.command_id}")
            self._refresh_phase()
            return {"ok": True, "outcome": "rejected",
                    "detail": f"{cmd.node_id} 永久拒绝，已停在人工处置位置"}
        self._emit("response_invalid", command_id=cmd.command_id, status=response.status)
        return {"ok": False, "outcome": "invalid",
                "detail": f"未知响应状态 {response.status!r}"}

    # ------------------------------------------------------------------
    # 时钟推进与超时
    # ------------------------------------------------------------------
    def tick(self) -> list[str]:
        """处理超时：在途命令超过时限即关闭，节点转为"结果未知"并阻塞。"""
        if self.phase in TERMINAL_PHASES or self.phase == Phase.DEGRADED:
            return []
        now = self.clock.now()
        expired = [
            cmd for cmd in self.commands.values()
            if cmd.status == CommandStatus.ISSUED and cmd.timeout_at <= now
        ]
        for cmd in expired:
            cmd.status = CommandStatus.TIMEOUT
            cmd.closed_at = now
            self._emit("command_timeout", command_id=cmd.command_id, node_id=cmd.node_id)
            self.node_state[cmd.node_id] = NodeState.UNCERTAIN
            self._emit("node_uncertain", node_id=cmd.node_id, reason=f"命令 {cmd.command_id} 超时")
        if expired:
            self._refresh_phase()
        return [cmd.command_id for cmd in expired]

    # ------------------------------------------------------------------
    # 暂停 / 恢复 / 接管
    # ------------------------------------------------------------------
    def pause(self) -> dict[str, Any]:
        if self.control != Control.AUTO:
            return {"ok": False, "outcome": "rejected", "detail": f"当前控制模式 {self.control.value} 不可暂停"}
        self.control = Control.PAUSED
        self._emit("control_changed", control=self.control.value)
        return {"ok": True, "outcome": "paused", "detail": "已暂停下发新命令，在途命令仍被跟踪"}

    def resume(self) -> dict[str, Any]:
        if self.control != Control.PAUSED:
            return {"ok": False, "outcome": "rejected", "detail": f"当前控制模式 {self.control.value} 不可恢复"}
        self.control = Control.AUTO
        self._emit("control_changed", control=self.control.value)
        self._evaluate()
        return {"ok": True, "outcome": "resumed", "detail": "已恢复自动推进"}

    def takeover(self, operator: str) -> dict[str, Any]:
        if self.control == Control.MANUAL:
            return {"ok": False, "outcome": "rejected", "detail": "已处于人工接管"}
        self.control = Control.MANUAL
        self._emit("control_changed", control=self.control.value, operator=operator)
        return {"ok": True, "outcome": "manual", "detail": f"{operator} 已接管，自动推进停止"}

    def release_to_auto(self, operator: str) -> dict[str, Any]:
        if self.control != Control.MANUAL:
            return {"ok": False, "outcome": "rejected", "detail": "当前不在人工接管"}
        self.control = Control.AUTO
        self._emit("control_changed", control=self.control.value, operator=operator)
        self._evaluate()
        return {"ok": True, "outcome": "auto", "detail": f"{operator} 交还自动控制"}

    # ------------------------------------------------------------------
    # 人工处置
    # ------------------------------------------------------------------
    def _manual_allowed(self) -> str | None:
        if self.phase in TERMINAL_PHASES:
            return f"恢复已{self.phase.value}，不再接受人工处置"
        if self.control == Control.AUTO and self.phase == Phase.RUNNING:
            return "自动运行中，请先 takeover() 接管或等待进入阻塞位置"
        return None

    def _requires_met(self, node: Node) -> bool:
        return all(self.node_state[req] == NodeState.READY for req in node.requires)

    def manual_mark(self, node_id: str, operator: str, rationale: str = "") -> dict[str, Any]:
        """操作员现场确认设备已操作完成，标记节点就绪。"""
        denial = self._manual_allowed()
        if denial:
            return {"ok": False, "outcome": "denied", "detail": denial}
        node = self.topology.get(node_id)
        if self.node_state[node_id] == NodeState.READY:
            return {"ok": False, "outcome": "denied", "detail": f"{node_id} 已就绪"}
        if not node.actionable:
            return {"ok": False, "outcome": "denied", "detail": f"{node_id} 为无源节点，由上游状态决定"}
        if not self._requires_met(node):
            unmet = [r for r in node.requires if self.node_state[r] != NodeState.READY]
            return {"ok": False, "outcome": "denied",
                    "detail": f"拓扑前置未满足：{', '.join(unmet)} 尚未就绪"}
        active = self.node_command.get(node_id)
        if active and self.commands[active].status == CommandStatus.ISSUED:
            cmd = self.commands[active]
            cmd.status = CommandStatus.ABORTED
            cmd.closed_at = self.clock.now()
            self._emit("command_aborted", command_id=active, node_id=node_id,
                       reason="人工现场确认，命令作废")
        self._emit("manual_mark", node_id=node_id, operator=operator, rationale=rationale)
        self._mark_ready(node_id, via="manual")
        self._refresh_phase()
        self._evaluate()
        return {"ok": True, "outcome": "marked", "detail": f"{node_id} 经 {operator} 现场确认就绪"}

    def manual_retry(self, node_id: str, operator: str) -> dict[str, Any]:
        """对被拒绝/结果未知的设备重新下达命令（新关联编号，可追溯旧命令）。"""
        denial = self._manual_allowed()
        if denial:
            return {"ok": False, "outcome": "denied", "detail": denial}
        if self.phase == Phase.DEGRADED:
            return {"ok": False, "outcome": "denied",
                    "detail": "市电已返回，受控降级期间不再下达孤网命令"}
        node = self.topology.get(node_id)
        if self.node_state[node_id] not in (NodeState.BLOCKED, NodeState.UNCERTAIN):
            return {"ok": False, "outcome": "denied",
                    "detail": f"{node_id} 当前状态 {self.node_state[node_id].value}，不可重试"}
        if not self._requires_met(node):
            unmet = [r for r in node.requires if self.node_state[r] != NodeState.READY]
            return {"ok": False, "outcome": "denied",
                    "detail": f"拓扑前置未满足：{', '.join(unmet)} 尚未就绪"}
        previous = self.node_command.get(node_id)
        cmd = self._issue(node, supersedes=previous)
        self._emit("manual_retry", node_id=node_id, operator=operator,
                   command_id=cmd.command_id, supersedes=previous)
        self._refresh_phase()
        self._evaluate()
        return {"ok": True, "outcome": "retried",
                "detail": f"已重新下达 {cmd.command_id}（取代 {previous}）",
                "command_id": cmd.command_id}

    # ------------------------------------------------------------------
    # 市电返回 / 整体中止
    # ------------------------------------------------------------------
    def utility_returned(self) -> dict[str, Any]:
        """市电提前返回：进入受控降级。

        在途孤网命令全部中止（关联编号保留，迟到的响应只入账），
        不再下达新的孤网命令；已确认的设备状态保持，等待并网规程处置。
        """
        if self.phase in TERMINAL_PHASES:
            return {"ok": False, "outcome": "denied", "detail": f"恢复已{self.phase.value}"}
        if self.phase == Phase.DEGRADED:
            return {"ok": False, "outcome": "denied", "detail": "已处于受控降级"}
        self._emit("utility_returned")
        self._abort_in_flight("市电提前返回，受控降级")
        self._set_phase(Phase.DEGRADED, "市电提前返回，停止孤网恢复，等待并网规程")
        return {"ok": True, "outcome": "degraded",
                "detail": "已进入受控降级：在途命令中止，不再下达孤网命令"}

    def abort(self, operator: str, reason: str = "") -> dict[str, Any]:
        if self.phase in TERMINAL_PHASES:
            return {"ok": False, "outcome": "denied", "detail": f"恢复已{self.phase.value}"}
        self._abort_in_flight(reason or "人工中止")
        self._set_phase(Phase.ABORTED, reason or "人工中止")
        self._emit("aborted", operator=operator, reason=reason)
        return {"ok": True, "outcome": "aborted", "detail": "恢复过程已中止"}

    # ------------------------------------------------------------------
    # 状态查询：为何等待、此刻允许哪些动作
    # ------------------------------------------------------------------
    def waiting_reasons(self, node_id: str) -> list[str]:
        state = self.node_state[node_id]
        node = self.topology.get(node_id)
        if state == NodeState.READY:
            return []
        if state == NodeState.COMMANDED:
            cid = self.node_command[node_id]
            cmd = self.commands[cid]
            remaining = max(0.0, cmd.timeout_at - self.clock.now())
            return [f"等待设备确认命令 {cid}（{remaining:.0f}s 后超时）"]
        if state == NodeState.UNCERTAIN:
            return ["命令结果未知：需人工确认现场状态，或补录设备响应，或显式重试"]
        if state == NodeState.BLOCKED:
            cid = self.node_command.get(node_id, "?")
            return [f"设备永久拒绝 {cid}：等待人工处置（重试 / 现场确认 / 中止）"]
        # PENDING
        unmet = [req for req in node.requires if self.node_state[req] != NodeState.READY]
        if unmet:
            return [
                f"等待上游 {req} 就绪（当前 {self.node_state[req].value}）"
                for req in unmet
            ]
        if self.phase == Phase.DEGRADED:
            return ["市电已返回，受控降级期间不再下达孤网命令"]
        if self.control == Control.PAUSED:
            return ["前置条件已满足，但协调器已暂停"]
        if self.control == Control.MANUAL:
            return ["前置条件已满足，等待接管人员下达动作"]
        return ["等待调度"]

    def allowed_actions(self) -> list[str]:
        if self.phase in TERMINAL_PHASES:
            return ["export"]
        actions: list[str] = []
        if self.phase != Phase.DEGRADED:
            if self.control == Control.AUTO:
                actions += ["pause", "takeover"]
            elif self.control == Control.PAUSED:
                actions += ["resume", "takeover"]
            else:
                actions += ["release_to_auto"]
        actions.append("inject_response")
        if self.phase == Phase.RUNNING:
            actions.append("utility_returned")
        for node in self.topology:
            if not node.actionable or not self._requires_met(node):
                continue
            state = self.node_state[node.id]
            if state in (NodeState.BLOCKED, NodeState.UNCERTAIN):
                if self.phase != Phase.DEGRADED:
                    actions.append(f"manual_retry:{node.id}")
                actions.append(f"manual_mark:{node.id}")
            elif (
                state == NodeState.PENDING
                and self.control == Control.MANUAL
                and self.phase == Phase.RUNNING
            ):
                actions.append(f"manual_mark:{node.id}")
        actions += ["abort", "export"]
        return actions

    def status(self) -> dict[str, Any]:
        return {
            "control": self.control.value,
            "phase": self.phase.value,
            "now": self.clock.now(),
            "nodes": [
                {
                    "id": node.id,
                    "kind": node.kind,
                    "state": self.node_state[node.id].value,
                    "ready_via": self.node_ready_via.get(node.id),
                    "active_command": (
                        self.node_command.get(node.id)
                        if self.node_state[node.id] == NodeState.COMMANDED
                        else None
                    ),
                    "waiting": self.waiting_reasons(node.id),
                }
                for node in self.topology
            ],
            "in_flight": [
                cmd.command_id for cmd in self.commands.values()
                if cmd.status == CommandStatus.ISSUED
            ],
            "allowed_actions": self.allowed_actions(),
        }

    # ------------------------------------------------------------------
    # 记录导出
    # ------------------------------------------------------------------
    def export_record(self) -> dict[str, Any]:
        from .report import build_record

        return build_record(self)

    def export(self, path: str) -> str:
        from .report import export_record

        export_record(self.export_record(), path)
        return path

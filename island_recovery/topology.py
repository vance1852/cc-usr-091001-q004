"""供电拓扑：节点、前置依赖与恢复计划的拓扑排序。

拓扑数据是评审通过的权威依据，协调器不会发明其中不存在的节点或顺序。
节点 kind 决定恢复阶段与设备命令：

- isolation  并网点隔离开关，必须最先确认断开
- source     构网型电源（构网型储能 PCS）
- bus        母线，自身带电条件由上游电源保证，不产生开合命令
- load       负荷，按 priority 升序恢复（数字越小越优先）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 恢复阶段顺序：隔离 -> 电源 -> 母线 -> 负荷。同阶段内按拓扑依赖与优先级。
STAGE_ORDER = {"isolation": 0, "source": 1, "bus": 2, "load": 3}


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    requires: tuple[str, ...]
    priority: int = 100
    description: str = ""
    # restorable=False 的节点（典型：柴油发电机）保留在拓扑图中供引用校验，
    # 但不进入自动恢复计划——它只能走人工程序，杜绝储能与柴油机被同时下令。
    restorable: bool = True

    @property
    def actionable(self) -> bool:
        """母线是条件节点而非可操作开关，不向设备下发命令。"""
        return self.kind in ("isolation", "source", "load")

    @property
    def command_verb(self) -> str:
        return {
            "isolation": "open",
            "source": "start",
            "load": "close",
        }[self.kind]


@dataclass(frozen=True)
class Topology:
    nodes: dict[str, Node] = field(default_factory=dict)

    def get(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def plan(self) -> list[Node]:
        """返回恢复顺序（不含 restorable=False 的人工程序节点）。

        排序键：(阶段, 最大上游阶段深度, 负荷优先级, 节点 id)。
        依赖闭包保证 requires 全部满足；同层取确定性顺序，便于演练复盘。
        """
        eligible = {nid for nid, n in self.nodes.items() if n.restorable}
        remaining = set(eligible)
        resolved: set[str] = set()
        depth_cache: dict[str, int] = {}

        def depth(nid: str) -> int:
            if nid in depth_cache:
                return depth_cache[nid]
            node = self.nodes[nid]
            value = 0 if not node.requires else 1 + max(depth(r) for r in node.requires)
            depth_cache[nid] = value
            return value

        ordered: list[Node] = []
        while remaining:
            ready = [
                self.nodes[nid]
                for nid in remaining
                if all(r in resolved for r in self.nodes[nid].requires)
            ]
            if not ready:
                cyclic = sorted(remaining)
                raise ValueError(f"拓扑存在无法满足的依赖（可能成环）：{cyclic}")
            ready.sort(key=lambda n: (STAGE_ORDER[n.kind], depth(n.id), n.priority, n.id))
            node = ready[0]
            ordered.append(node)
            remaining.remove(node.id)
            resolved.add(node.id)

        # 阶段不得倒序：隔离之前不允许出现电源/负荷命令。
        stages = [STAGE_ORDER[n.kind] for n in ordered]
        if stages != sorted(stages):
            raise ValueError("拓扑排序违反隔离→电源→母线→负荷的阶段约束")
        return ordered


def load_topology(source: str | Path | dict[str, Any]) -> Topology:
    """从 JSON 文件路径或已解析的字典载入拓扑，并做引用合法性校验。"""
    if isinstance(source, dict):
        data = source
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))

    nodes: dict[str, Node] = {}
    for raw in data.get("nodes", []):
        kind = raw["kind"]
        if kind not in STAGE_ORDER:
            raise ValueError(f"节点 {raw['id']} 的类型未知：{kind}")
        node = Node(
            id=raw["id"],
            kind=kind,
            requires=tuple(raw.get("requires", [])),
            priority=int(raw.get("priority", 100)),
            description=str(raw.get("description", "")),
            restorable=bool(raw.get("restorable", True)),
        )
        if node.id in nodes:
            raise ValueError(f"节点 id 重复：{node.id}")
        nodes[node.id] = node

    for node in nodes.values():
        for ref in node.requires:
            if ref not in nodes:
                raise ValueError(f"节点 {node.id} 引用了不存在的依赖：{ref}")

    # 自动恢复链上的节点，其上游也必须可自动恢复；否则门禁永远无法满足，
    # 更糟的是会诱使协调器对柴油机等互锁设备下令。
    for node in nodes.values():
        if node.restorable:
            bad = [r for r in node.requires if not nodes[r].restorable]
            if bad:
                raise ValueError(
                    f"自动恢复节点 {node.id} 依赖仅人工程序的节点 {bad}，"
                    "存在设备互锁风险，请修改拓扑"
                )

    # 安全原则的结构性约束：
    # 电源必须（传递地）受隔离开关约束；母线必须挂在电源之后；
    # 负荷必须挂在母线之后——普通负荷不得早于关键母线恢复。
    def ancestors(nid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(nodes[nid].requires)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(nodes[cur].requires)
        return seen

    required_upstream = {"source": {"isolation"}, "bus": {"source"}, "load": {"bus"}}
    for node in nodes.values():
        if not node.restorable:
            continue
        kinds = {nodes[a].kind for a in ancestors(node.id)}
        for needed in required_upstream.get(node.kind, set()):
            if needed not in kinds:
                raise ValueError(
                    f"节点 {node.id}（{node.kind}）的上游链中缺少 {needed} 节点，"
                    "违反隔离→电源→母线→负荷的安全原则"
                )

    topology = Topology(nodes=nodes)
    topology.plan()  # 提前暴露成环/阶段冲突
    return topology

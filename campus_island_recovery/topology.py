"""评审通过的供电拓扑：节点、前置条件与送电优先级。

`requires` 表示送电前必须已经稳定的上游：隔离开关确认后才能启动
构网型电源，电源稳定后母线才视为带电，普通负荷不得早于关键母线
恢复。母线等无源节点不下发命令，上游就绪即视为就绪。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

#: 评审通过的节点类型
KINDS = ("isolation", "source", "bus", "load")

#: 无源节点：不需要下发命令，上游全部就绪即视为就绪
PASSIVE_KINDS = frozenset({"bus"})

#: 节点类型到设备命令动词的映射（握手协议的一部分）
COMMAND_VERBS = {"isolation": "open", "source": "start", "load": "close"}


class TopologyError(ValueError):
    """拓扑未通过评审校验。"""


@dataclass(frozen=True)
class Node:
    """供电拓扑中的一个节点。

    priority 仅在同为待调度的可命令节点之间起作用，数值小者先送电。
    """

    id: str
    kind: str
    requires: tuple[str, ...] = ()
    priority: int = 100

    @property
    def actionable(self) -> bool:
        """是否需要协调器下发设备命令。"""
        return self.kind not in PASSIVE_KINDS


class Topology:
    """经过校验的供电拓扑。"""

    def __init__(self, nodes: Iterable[Node]):
        self._nodes = list(nodes)
        ids = [n.id for n in self._nodes]
        if len(set(ids)) != len(ids):
            raise TopologyError("节点 id 重复")
        by_id = {n.id: n for n in self._nodes}
        for node in self._nodes:
            if node.kind not in KINDS:
                raise TopologyError(f"未知节点类型 {node.kind!r}（节点 {node.id}）")
            for req in node.requires:
                if req not in by_id:
                    raise TopologyError(f"节点 {node.id} 依赖不存在的节点 {req!r}")
        self._by_id = by_id
        self._check_acyclic()

    @classmethod
    def from_dict(cls, data: dict) -> "Topology":
        """从评审通过的拓扑文件（如演练 JSON 的 nodes 段）构建。"""
        nodes = [
            Node(
                id=item["id"],
                kind=item["kind"],
                requires=tuple(item.get("requires", ())),
                priority=int(item.get("priority", 100)),
            )
            for item in data["nodes"]
        ]
        return cls(nodes)

    def _check_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise TopologyError(f"拓扑存在环，涉及节点 {node_id!r}")
            visiting.add(node_id)
            for req in self._by_id[node_id].requires:
                walk(req)
            visiting.discard(node_id)
            visited.add(node_id)

        for node in self._nodes:
            walk(node.id)

    def __iter__(self) -> Iterator[Node]:
        return iter(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def get(self, node_id: str) -> Node:
        try:
            return self._by_id[node_id]
        except KeyError:
            raise TopologyError(f"未知节点 {node_id!r}") from None

    def candidates(self, is_ready, is_pending) -> list[Node]:
        """当前可下达命令的节点：可命令、仍待处理、上游全部就绪。

        返回按 (priority, id) 排序，保证负荷严格按优先级依次送电。
        """
        ready = [
            node
            for node in self._nodes
            if node.actionable
            and is_pending(node.id)
            and all(is_ready(req) for req in node.requires)
        ]
        return sorted(ready, key=lambda n: (n.priority, n.id))

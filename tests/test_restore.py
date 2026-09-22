import tempfile
import unittest
from pathlib import Path

from campus_island_recovery import Coordinator, Journal, Response, Topology
from campus_island_recovery.clock import VirtualClock
from campus_island_recovery.coordinator import NodeState, Phase
from campus_island_recovery.protocol import CommandStatus

FOUR_NODE = {
    "nodes": [
        {"id": "grid-breaker", "kind": "isolation", "requires": []},
        {"id": "forming-pcs", "kind": "source", "requires": ["grid-breaker"]},
        {"id": "critical-bus", "kind": "bus", "requires": ["forming-pcs"]},
        {"id": "cold-store", "kind": "load", "requires": ["critical-bus"], "priority": 1},
    ]
}


def confirm(coord, command_id, device):
    return coord.inject_response(Response(command_id=command_id, device=device, status="confirmed"))


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal_path = str(Path(self.tmp.name) / "journal.jsonl")
        # 崩溃前：隔离已确认，电源启动命令在途
        coord = Coordinator(
            Topology.from_dict(FOUR_NODE), Journal(self.journal_path), VirtualClock()
        )
        confirm(coord, "open-01", "grid-breaker")
        self.assertIn("start-02", coord.commands)
        coord.journal.close()

    def _restore(self):
        coord = Coordinator.restore(
            Topology.from_dict(FOUR_NODE), self.journal_path, VirtualClock()
        )
        self.addCleanup(coord.journal.close)
        return coord

    def test_confirmed_switch_never_reoperated_after_restart(self):
        coord = self._restore()
        # 已确认的开关保持就绪，绝不再次下令
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.READY)
        open_commands = [
            e for e in coord.journal.events
            if e["type"] == "command_issued" and e.get("verb") == "open"
        ]
        self.assertEqual(len(open_commands), 1)
        # 崩溃时在途的命令转为"结果未知"，阻塞自动推进
        self.assertEqual(coord.commands["start-02"].status, CommandStatus.UNCERTAIN)
        self.assertEqual(coord.node_state["forming-pcs"], NodeState.UNCERTAIN)
        self.assertEqual(coord.phase, Phase.BLOCKED)
        self.assertNotIn("close-03", coord.commands)

    def test_in_flight_command_resolvable_by_delayed_response(self):
        coord = self._restore()
        # 补录设备迟到的响应即可解除不确定，恢复自动推进
        result = confirm(coord, "start-02", "forming-pcs")
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(coord.node_state["forming-pcs"], NodeState.READY)
        self.assertEqual(coord.node_state["critical-bus"], NodeState.READY)
        self.assertEqual(coord.phase, Phase.RUNNING)
        self.assertIn("close-03", coord.commands)
        confirm(coord, "close-03", "cold-store")
        self.assertEqual(coord.phase, Phase.COMPLETED)

    def test_command_sequence_continues_after_restart(self):
        coord = self._restore()
        # 命令编号不复用：人工重试在途不明的电源，编号继续递增
        result = coord.manual_retry("forming-pcs", operator="值班长")
        self.assertTrue(result["ok"])
        self.assertEqual(result["command_id"], "start-03")
        self.assertEqual(coord.commands["start-03"].supersedes, "start-02")
        confirm(coord, "start-03", "forming-pcs")
        confirm(coord, "close-04", "cold-store")
        self.assertEqual(coord.phase, Phase.COMPLETED)
        # 全程动作次序仍受拓扑前置约束
        issued = [
            e["node_id"] for e in coord.journal.events if e["type"] == "command_issued"
        ]
        self.assertEqual(
            issued, ["grid-breaker", "forming-pcs", "forming-pcs", "cold-store"]
        )

    def test_restart_after_completion_stays_completed(self):
        coord = self._restore()
        confirm(coord, "start-02", "forming-pcs")
        confirm(coord, "close-03", "cold-store")
        self.assertEqual(coord.phase, Phase.COMPLETED)
        again = self._restore()
        self.assertEqual(again.phase, Phase.COMPLETED)
        self.assertEqual(again.allowed_actions(), ["export"])
        self.assertEqual(len(again.commands), 3)


if __name__ == "__main__":
    unittest.main()

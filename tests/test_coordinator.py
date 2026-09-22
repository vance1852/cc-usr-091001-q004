import tempfile
import unittest
from pathlib import Path

from campus_island_recovery import Coordinator, Journal, Response, Topology
from campus_island_recovery.clock import VirtualClock
from campus_island_recovery.coordinator import Control, NodeState, Phase
from campus_island_recovery.protocol import CommandStatus

FOUR_NODE = {
    "nodes": [
        {"id": "grid-breaker", "kind": "isolation", "requires": []},
        {"id": "forming-pcs", "kind": "source", "requires": ["grid-breaker"]},
        {"id": "critical-bus", "kind": "bus", "requires": ["forming-pcs"]},
        {"id": "cold-store", "kind": "load", "requires": ["critical-bus"], "priority": 1},
    ]
}

BRANCHED = {
    "nodes": [
        {"id": "grid-breaker", "kind": "isolation", "requires": []},
        {"id": "forming-pcs", "kind": "source", "requires": ["grid-breaker"]},
        {"id": "critical-bus", "kind": "bus", "requires": ["forming-pcs"]},
        {"id": "cold-store", "kind": "load", "requires": ["critical-bus"], "priority": 1},
        {"id": "lighting", "kind": "load", "requires": ["critical-bus"], "priority": 2},
    ]
}


class CoordinatorCase(unittest.TestCase):
    def make_coordinator(self, data=FOUR_NODE, timeout=30.0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Journal(Path(tmp.name) / "journal.jsonl")
        self.addCleanup(journal.close)
        clock = VirtualClock()
        coord = Coordinator(Topology.from_dict(data), journal, clock, command_timeout=timeout)
        return coord, clock


def confirm(coord, command_id, device):
    return coord.inject_response(Response(command_id=command_id, device=device, status="confirmed"))


class TopologyOrderTest(CoordinatorCase):
    def test_command_sequence_follows_topology(self):
        coord, _ = self.make_coordinator()
        # 隔离确认前不得启动电源
        self.assertEqual(list(coord.commands), ["open-01"])
        confirm(coord, "open-01", "grid-breaker")
        self.assertEqual(list(coord.commands), ["open-01", "start-02"])
        confirm(coord, "start-02", "forming-pcs")
        # 母线为无源节点，电源稳定后自动就绪，随后才送负荷
        self.assertEqual(coord.node_state["critical-bus"], NodeState.READY)
        self.assertEqual(list(coord.commands), ["open-01", "start-02", "close-03"])
        result = confirm(coord, "close-03", "cold-store")
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(coord.phase, Phase.COMPLETED)

    def test_loads_energized_by_priority_and_out_of_order_replies(self):
        coord, _ = self.make_coordinator(BRANCHED)
        confirm(coord, "open-01", "grid-breaker")
        confirm(coord, "start-02", "forming-pcs")
        # 两个负荷按优先级先后下令
        self.assertEqual(list(coord.commands), ["open-01", "start-02", "close-03", "close-04"])
        self.assertEqual(coord.commands["close-03"].node_id, "cold-store")
        self.assertEqual(coord.commands["close-04"].node_id, "lighting")
        # 乱序回复：后下令的先确认，不影响仍在途的命令
        confirm(coord, "close-04", "lighting")
        self.assertEqual(coord.node_state["lighting"], NodeState.READY)
        self.assertEqual(coord.node_state["cold-store"], NodeState.COMMANDED)
        self.assertEqual(coord.phase, Phase.RUNNING)
        confirm(coord, "close-03", "cold-store")
        self.assertEqual(coord.phase, Phase.COMPLETED)


class ResponseCorrelationTest(CoordinatorCase):
    def test_orphan_and_duplicate_responses_do_not_advance(self):
        coord, _ = self.make_coordinator()
        # 未知关联编号：入账但不推进
        result = coord.inject_response(Response("close-99", "cold-store", "confirmed"))
        self.assertEqual(result["outcome"], "orphan")
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.COMMANDED)
        self.assertEqual(list(coord.commands), ["open-01"])
        # 正常确认后重复确认：只入账
        confirm(coord, "open-01", "grid-breaker")
        ready_events = [
            e for e in coord.journal.events
            if e["type"] == "node_ready" and e["node_id"] == "grid-breaker"
        ]
        self.assertEqual(len(ready_events), 1)
        result = confirm(coord, "open-01", "grid-breaker")
        self.assertEqual(result["outcome"], "duplicate")
        ready_events = [
            e for e in coord.journal.events
            if e["type"] == "node_ready" and e["node_id"] == "grid-breaker"
        ]
        self.assertEqual(len(ready_events), 1)
        # 响应设备与命令对象不符：拒绝入账推进
        result = coord.inject_response(Response("start-02", "cold-store", "confirmed"))
        self.assertEqual(result["outcome"], "mismatch")
        self.assertEqual(coord.node_state["forming-pcs"], NodeState.COMMANDED)

    def test_timeout_blocks_and_late_reply_does_not_advance(self):
        coord, clock = self.make_coordinator(timeout=10.0)
        clock.advance(11.0)
        expired = coord.tick()
        self.assertEqual(expired, ["open-01"])
        self.assertEqual(coord.commands["open-01"].status, CommandStatus.TIMEOUT)
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.UNCERTAIN)
        self.assertEqual(coord.phase, Phase.BLOCKED)
        # 迟到的确认不误推进：节点保持"结果未知"，等待人工处置
        result = confirm(coord, "open-01", "grid-breaker")
        self.assertEqual(result["outcome"], "late")
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.UNCERTAIN)
        actions = coord.allowed_actions()
        self.assertIn("manual_retry:grid-breaker", actions)
        self.assertIn("manual_mark:grid-breaker", actions)


class RejectionAndManualTest(CoordinatorCase):
    def _reach_load_command(self, coord):
        confirm(coord, "open-01", "grid-breaker")
        confirm(coord, "start-02", "forming-pcs")

    def test_rejection_halts_at_manual_position_then_retry(self):
        coord, _ = self.make_coordinator()
        self._reach_load_command(coord)
        result = coord.inject_response(
            Response("close-03", "cold-store", "rejected", detail="开关互锁")
        )
        self.assertEqual(result["outcome"], "rejected")
        # 永久拒绝：停在允许人工处置的位置
        self.assertEqual(coord.phase, Phase.BLOCKED)
        self.assertEqual(coord.node_state["cold-store"], NodeState.BLOCKED)
        self.assertIn("拒绝", coord.waiting_reasons("cold-store")[0])
        actions = coord.allowed_actions()
        self.assertIn("manual_retry:cold-store", actions)
        self.assertIn("manual_mark:cold-store", actions)
        self.assertIn("abort", actions)
        # 人工重试：新关联编号可追溯旧命令
        result = coord.manual_retry("cold-store", operator="值班长")
        self.assertTrue(result["ok"])
        self.assertEqual(result["command_id"], "close-04")
        self.assertEqual(coord.commands["close-04"].supersedes, "close-03")
        self.assertEqual(coord.phase, Phase.RUNNING)
        confirm(coord, "close-04", "cold-store")
        self.assertEqual(coord.phase, Phase.COMPLETED)

    def test_manual_mark_requires_preconditions_and_takeover(self):
        coord, _ = self.make_coordinator()
        # 自动运行中不允许直接人工标记
        result = coord.manual_mark("grid-breaker", operator="值班长")
        self.assertFalse(result["ok"])
        coord.takeover("值班长")
        # 拓扑前置未满足时不允许越级标记
        result = coord.manual_mark("cold-store", operator="值班长")
        self.assertFalse(result["ok"])
        self.assertIn("前置", result["detail"])
        # 逐级人工确认
        self.assertTrue(coord.manual_mark("grid-breaker", operator="值班长")["ok"])
        self.assertTrue(coord.manual_mark("forming-pcs", operator="值班长")["ok"])
        self.assertEqual(coord.node_state["critical-bus"], NodeState.READY)
        self.assertIn("manual_mark:cold-store", coord.allowed_actions())
        self.assertTrue(coord.manual_mark("cold-store", operator="值班长")["ok"])
        self.assertEqual(coord.phase, Phase.COMPLETED)
        self.assertEqual(coord.node_ready_via["cold-store"], "manual")


class PauseResumeTest(CoordinatorCase):
    def test_pause_blocks_issuance_resume_continues(self):
        coord, _ = self.make_coordinator()
        self.assertTrue(coord.pause()["ok"])
        confirm(coord, "open-01", "grid-breaker")
        # 暂停期间在途命令照常确认，但不下发新命令
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.READY)
        self.assertNotIn("start-02", coord.commands)
        self.assertIn("前置条件已满足，但协调器已暂停", coord.waiting_reasons("forming-pcs"))
        self.assertTrue(coord.resume()["ok"])
        self.assertIn("start-02", coord.commands)

    def test_takeover_stops_auto_and_release_resumes(self):
        coord, _ = self.make_coordinator()
        coord.takeover("值班长")
        self.assertEqual(coord.control, Control.MANUAL)
        confirm(coord, "open-01", "grid-breaker")
        self.assertNotIn("start-02", coord.commands)
        coord.release_to_auto("值班长")
        self.assertIn("start-02", coord.commands)


class UtilityReturnTest(CoordinatorCase):
    def test_utility_return_triggers_controlled_degradation(self):
        coord, _ = self.make_coordinator()
        confirm(coord, "open-01", "grid-breaker")
        result = coord.utility_returned()
        self.assertEqual(result["outcome"], "degraded")
        self.assertEqual(coord.phase, Phase.DEGRADED)
        # 在途命令被中止，关联保留
        self.assertEqual(coord.commands["start-02"].status, CommandStatus.ABORTED)
        self.assertEqual(coord.node_state["forming-pcs"], NodeState.UNCERTAIN)
        # 已确认的开关保持原状
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.READY)
        # 迟到响应只入账，不再推进
        result = confirm(coord, "start-02", "forming-pcs")
        self.assertEqual(result["outcome"], "late")
        self.assertEqual(len(coord.commands), 2)
        # 降级期间不再下达孤网命令
        actions = coord.allowed_actions()
        self.assertNotIn("utility_returned", actions)
        self.assertNotIn("manual_retry:forming-pcs", actions)
        self.assertIn("manual_mark:forming-pcs", actions)
        self.assertIn("abort", actions)
        types = {e["type"] for e in coord.journal.events}
        self.assertIn("utility_returned", types)
        self.assertIn("command_aborted", types)


class ExportTest(CoordinatorCase):
    def test_record_contains_decisions_and_manual_actions(self):
        coord, _ = self.make_coordinator()
        confirm(coord, "open-01", "grid-breaker")
        coord.inject_response(Response("start-02", "forming-pcs", "rejected"))
        coord.manual_mark("forming-pcs", operator="值班长", rationale="现场手动启机")
        confirm(coord, "close-03", "cold-store")
        record = coord.export_record()
        types = [e["type"] for e in record["events"]]
        self.assertIn("command_issued", types)
        self.assertIn("command_rejected", types)
        self.assertIn("manual_mark", types)
        self.assertIn("completed", types)
        self.assertTrue(record["manual_interventions"])
        self.assertEqual(record["final_status"]["phase"], "completed")


if __name__ == "__main__":
    unittest.main()

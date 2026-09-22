import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from campus_island_recovery import DrillRunner
from campus_island_recovery.cli import main as cli_main
from campus_island_recovery.coordinator import NodeState, Phase
from campus_island_recovery.report import render_record_text

DRILL_PATH = Path(__file__).parents[1] / "reference" / "recovery_drill.json"


class ReferenceDrillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = str(Path(self.tmp.name) / "journal.jsonl")

    def test_reference_drill_end_to_end(self):
        runner = DrillRunner(str(DRILL_PATH), self.journal)
        self.addCleanup(runner.journal.close)
        coord = runner.coordinator
        # 逐条注入既有响应
        self.assertEqual(runner.inject_next()["outcome"], "confirmed")   # open-01
        self.assertEqual(runner.inject_next()["outcome"], "confirmed")   # start-02
        result = runner.inject_next()                                    # close-03 被拒
        self.assertEqual(result["outcome"], "rejected")
        # 永久拒绝：停在允许人工处置的位置，负荷等待原因可见
        self.assertEqual(coord.phase, Phase.BLOCKED)
        self.assertEqual(coord.node_state["cold-store"], NodeState.BLOCKED)
        self.assertIn("拒绝", coord.waiting_reasons("cold-store")[0])
        self.assertIn("manual_retry:cold-store", coord.allowed_actions())
        # 演练时钟按响应 delay 推进
        self.assertEqual(coord.clock.now(), 23.0)
        # 人工现场确认后完成恢复
        result = coord.manual_mark("cold-store", operator="值班长", rationale="现场合闸完成")
        self.assertTrue(result["ok"])
        self.assertEqual(coord.phase, Phase.COMPLETED)
        # 导出完整恢复记录：自动决策与人工处置俱在
        record = coord.export_record()
        types = [e["type"] for e in record["events"]]
        for expected in ("command_issued", "command_confirmed", "command_rejected",
                         "node_blocked", "manual_mark", "completed"):
            self.assertIn(expected, types)
        self.assertTrue(record["manual_interventions"])
        text = render_record_text(record)
        self.assertIn("manual_mark", text)
        self.assertIn("cold-store", text)

    def test_drill_resume_from_journal(self):
        runner = DrillRunner(str(DRILL_PATH), self.journal)
        runner.inject_next()  # open-01 确认后"进程崩溃"
        runner.journal.close()
        resumed = DrillRunner.resume(str(DRILL_PATH), self.journal)
        self.addCleanup(resumed.journal.close)
        coord = resumed.coordinator
        self.assertEqual(coord.node_state["grid-breaker"], NodeState.READY)
        self.assertEqual(coord.node_state["forming-pcs"], NodeState.UNCERTAIN)
        # 游标跳过已消费的 open-01，下一条注入 start-02 的响应并解除不确定
        self.assertEqual(resumed.next_response()["command_id"], "start-02")
        self.assertEqual(resumed.inject_next()["outcome"], "resolved")
        self.assertEqual(resumed.inject_next()["outcome"], "rejected")
        self.assertEqual(coord.phase, Phase.BLOCKED)

    def test_misaligned_response_is_reported(self):
        data = json.loads(DRILL_PATH.read_text(encoding="utf-8"))
        data["responses"] = [
            {"command_id": "close-99", "device": "cold-store", "status": "confirmed"}
        ]
        runner = DrillRunner(data, self.journal)
        self.addCleanup(runner.journal.close)
        result = runner.inject_next()
        self.assertEqual(result["outcome"], "drill_misaligned")

    def test_response_arriving_after_timeout_is_late(self):
        data = json.loads(DRILL_PATH.read_text(encoding="utf-8"))
        runner = DrillRunner(data, self.journal, command_timeout=10.0)
        self.addCleanup(runner.journal.close)
        self.assertEqual(runner.inject_next()["outcome"], "confirmed")  # open-01，3s
        # start-02 的响应 18s 后才到，超过 10s 超时：先判超时，响应记为迟到
        result = runner.inject_next()
        self.assertEqual(result["outcome"], "late")
        self.assertEqual(runner.coordinator.node_state["forming-pcs"], NodeState.UNCERTAIN)
        self.assertEqual(runner.coordinator.phase, Phase.BLOCKED)


class CliTest(unittest.TestCase):
    def test_auto_run_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = str(Path(tmp) / "journal.jsonl")
            record_path = str(Path(tmp) / "record.json")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli_main([str(DRILL_PATH), "--journal", journal, "--auto",
                               "--export", record_path])
            self.assertEqual(rc, 0)
            record = json.loads(Path(record_path).read_text(encoding="utf-8"))
            self.assertEqual(record["final_status"]["phase"], "blocked")
            self.assertTrue(Path(record_path).with_suffix(".txt").exists())


if __name__ == "__main__":
    unittest.main()

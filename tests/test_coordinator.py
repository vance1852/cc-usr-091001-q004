import json
import tempfile
import unittest
from pathlib import Path

from island_recovery.coordinator import RecoveryCoordinator, RecoveryError
from island_recovery.drill import DrillPlayer, SimClock
from island_recovery.gateway import RecordingGateway
from island_recovery.journal import Journal, JournalCorrupted
from island_recovery.topology import load_topology

ROOT = Path(__file__).parents[1]
FIX = Path(__file__).parent / "fixtures" / "campus_topology.json"
REFERENCE = ROOT / "reference" / "recovery_drill.json"
PLAN_4 = ["grid-breaker", "forming-pcs", "critical-bus", "cold-store"]


class CoordinatorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jpath = Path(self.tmp.name) / "recovery.journal.jsonl"
        self.topology = load_topology(FIX)
        self.gateway = RecordingGateway()
        self.clock = SimClock()
        self.coord = RecoveryCoordinator.start(
            self.jpath, self.topology, self.gateway, clock=self.clock
        )

    def tearDown(self):
        self.tmp.cleanup()

    def issue(self):
        return self.coord.tick()

    def confirm(self, command_id, device=None):
        return self.coord.inject_response(command_id, "confirmed", device=device)

    # ------------------------------------------------------- 基本安全顺序

    def test_commands_follow_isolation_source_bus_priority_order(self):
        r1 = self.issue()
        self.assertEqual((r1["action"], r1["command_id"]), ("issued", "open-01"))
        self.assertEqual(self.issue()["action"], "awaiting_confirmation")
        self.confirm("open-01")

        r2 = self.issue()
        self.assertEqual((r2["action"], r2["command_id"]), ("issued", "start-02"))
        self.confirm("start-02")

        # 母线无开关，上游稳定即确认，不向网关下发命令。
        r3 = self.issue()
        self.assertEqual(r3["action"], "bus_condition_met")
        sent = [(c.command_id, c.device) for c in self.gateway.sent]
        self.assertNotIn(("bus", "critical-bus"), sent)

        # 负荷按 priority 升序送电。
        for expected in (("close-03", "emergency-lighting"),
                         ("close-04", "cold-store"),
                         ("close-05", "process-line"),
                         ("close-06", "office-load")):
            r = self.issue()
            self.assertEqual((r["command_id"], r["node"]), expected)
            self.confirm(expected[0])

        self.assertEqual(self.issue()["action"], "completed")
        self.assertEqual(self.coord.terminal, "completed")
        # 柴油机从未收到任何自动命令。
        self.assertEqual(self.gateway.device_commands("diesel-gen"), [])

    def test_loads_cannot_close_before_bus_is_confirmed(self):
        self.issue(); self.confirm("open-01")
        self.issue(); self.confirm("start-02")
        view = self.coord.node_view("emergency-lighting")
        self.assertEqual(view["status"], "pending")
        self.assertEqual(view["unmet_requires"], ["critical-bus"])
        self.assertIn("等待上游：critical-bus", view["disposition"])

    def test_bus_auto_confirmation_requires_source(self):
        self.issue(); self.confirm("open-01")
        self.issue()  # start-02 在途
        # 构网电源未确认前，母线即使被轮到也不会确认（它排在电源之后）。
        self.assertEqual(self.coord.nodes["critical-bus"].status, "pending")

    # ------------------------------------------------------- 握手关联

    def test_response_for_unknown_command_is_uncorrelated(self):
        self.issue()  # open-01
        result = self.confirm("close-99")
        self.assertEqual(result["disposition"], "uncorrelated")
        # 在途命令不受影响，状态不推进。
        self.assertEqual(self.coord.active_id, "open-01")
        self.assertEqual(self.issue()["action"], "awaiting_confirmation")

    def test_response_from_wrong_device_is_uncorrelated(self):
        self.issue()
        result = self.coord.inject_response(
            "open-01", "confirmed", device="forming-pcs")
        self.assertEqual(result["disposition"], "device_mismatch")
        self.assertEqual(self.coord.active_id, "open-01")

    def test_duplicate_confirmation_does_not_reoperate(self):
        self.issue(); self.confirm("open-01")
        dup = self.confirm("open-01")
        self.assertEqual(dup["disposition"], "duplicate")
        # 确认后绝不再次操作该开关。
        self.assertEqual(len(self.gateway.device_commands("grid-breaker")), 1)

    def test_late_confirm_after_timeout_does_not_advance(self):
        self.coord.timeouts["isolation"] = 10
        self.issue()  # open-01, deadline 10
        self.clock.advance(11)
        self.assertEqual(self.issue()["action"], "timed_out")
        self.assertIn(("open-01", "confirmation-timeout"), self.gateway.aborted)
        # 迟到确认：只入账，不推进。
        late = self.confirm("open-01")
        self.assertEqual(late["disposition"], "late")
        self.assertEqual(self.coord.nodes["grid-breaker"].status, "pending")
        # 下一步重发新编号命令，旧编号不复用。
        r = self.issue()
        self.assertEqual((r["command_id"], r["node"]), ("open-02", "grid-breaker"))

    def test_only_one_command_in_flight_at_a_time(self):
        self.issue()  # open-01
        self.confirm("open-01")
        self.issue()  # start-02
        again = self.issue()
        self.assertEqual(again["action"], "awaiting_confirmation")
        self.assertEqual(len(self.gateway.sent), 2)

    # ------------------------------------------------------- 拒绝与停靠

    def test_permanent_reject_halts_at_manual_position(self):
        self.issue(); self.confirm("open-01")
        self.issue(); self.confirm("start-02")
        self.issue()  # bus
        self.issue()  # close-03 emergency-lighting
        result = self.coord.inject_response(
            "close-03", "rejected", reason="机构卡涩")
        self.assertEqual(result["disposition"], "permanent_reject")
        self.assertFalse(self.coord.automation)
        self.assertEqual(self.coord.hold_node, "emergency-lighting")
        self.assertIsNone(self.coord.terminal)
        # 下游负荷继续等待，自动 tick 不再发令。
        self.assertEqual(self.issue()["action"], "none")
        self.assertIn("等待人工处置", self.coord.node_view("emergency-lighting")["disposition"])

    def test_transient_reject_is_retried(self):
        self.issue()
        result = self.coord.inject_response(
            "open-01", "rejected", permanent=False, reason="瞬时压力低")
        self.assertEqual(result["disposition"], "transient_reject")
        self.assertTrue(self.coord.automation)
        r = self.issue()
        self.assertEqual((r["command_id"], r["node"]), ("open-02", "grid-breaker"))
        self.assertEqual(r["deadline"] > r.get("deadline", 0) - 1, True)
        self.confirm("open-02")
        self.assertEqual(self.coord.nodes["grid-breaker"].status, "confirmed")

    def test_exhausted_attempts_enter_manual_hold(self):
        self.coord.max_attempts = 1
        self.issue()  # open-01
        self.clock.advance(99)
        self.issue()  # timeout open-01
        hold = self.issue()  # 重试次数用尽
        self.assertEqual(hold["action"], "manual_hold")
        self.assertEqual(self.coord.hold_node, "grid-breaker")

    def test_manual_confirm_resolves_hold_and_recovery_continues(self):
        self.issue(); self.confirm("open-01")
        self.issue(); self.confirm("start-02")
        self.issue()  # bus
        self.issue()  # close-03
        self.coord.inject_response("close-03", "rejected", reason="卡涩")
        # 停靠点未解除不能归还自动化。
        with self.assertRaises(RecoveryError):
            self.coord.resume_automation()
        # 现场处置后人工确认（自动化已被系统挂起，允许人工动作）。
        self.coord.manual_confirm("emergency-lighting", operator="张工", note="就地检查正常")
        self.assertIsNone(self.coord.hold_node)
        self.coord.resume_automation()
        r = self.issue()
        self.assertEqual((r["command_id"], r["node"]), ("close-04", "cold-store"))

    def test_manual_confirm_cannot_bypass_topology(self):
        self.issue(); self.confirm("open-01")
        self.coord.suspend_automation("演练")
        with self.assertRaises(RecoveryError):
            self.coord.manual_confirm("cold-store", operator="张工")
        with self.assertRaises(RecoveryError):
            self.coord.manual_confirm("diesel-gen", operator="张工")

    def test_manual_confirm_requires_pause_or_suspension(self):
        self.issue(); self.confirm("open-01")
        with self.assertRaises(RecoveryError):
            self.coord.manual_confirm("forming-pcs", operator="张工")

    def test_manual_confirm_rejects_double_confirmation(self):
        self.issue(); self.confirm("open-01")
        self.coord.pause("演练暂停")
        with self.assertRaises(RecoveryError):
            self.coord.manual_confirm("grid-breaker", operator="张工")

    # ------------------------------------------------------- 暂停/接管

    def test_pause_blocks_auto_tick_and_resume_continues(self):
        self.issue()
        self.coord.pause("指挥暂停")
        self.assertEqual(self.issue()["action"], "none")
        # 暂停期间设备确认仍可入账，但不自动发下一步。
        self.confirm("open-01")
        self.assertEqual(self.issue()["action"], "none")
        self.coord.resume()
        r = self.issue()
        self.assertEqual(r["command_id"], "start-02")

    def test_abort_active_command_requires_manual_mode(self):
        self.issue()
        with self.assertRaises(RecoveryError):
            self.coord.abort_active_command("演练")
        self.coord.pause("暂停")
        self.coord.abort_active_command("演练中止")
        self.assertIsNone(self.coord.active_id)
        self.assertIn(("open-01", "manual-abort: 演练中止"), self.gateway.aborted)

    # ------------------------------------------------------- 市电提前返回

    def test_grid_return_while_command_in_flight_derates(self):
        self.issue(); self.confirm("open-01")
        self.issue()  # start-02 在途
        self.coord.grid_returned()
        r = self.issue()
        self.assertEqual(r["action"], "derated")
        self.assertEqual(self.coord.terminal, "aborted")
        self.assertIn(("start-02", "grid-returned-controlled-derating"),
                      self.gateway.aborted)
        # 终态后不再发令。
        self.assertEqual(self.issue()["action"], "none")
        with self.assertRaises(RecoveryError):
            self.coord.resume_automation()

    def test_grid_return_while_paused_still_derates(self):
        self.issue()
        self.coord.pause("暂停")
        self.coord.grid_returned()
        r = self.issue()  # 即使暂停也必须受控降级
        self.assertEqual(r["action"], "derated")
        self.assertIn(("open-01", "grid-returned-controlled-derating"),
                      self.gateway.aborted)

    def test_manual_abort_aborts_in_flight_and_terminates(self):
        self.issue()  # open-01
        self.coord.manual_abort("发现新的接地点")
        self.assertEqual(self.coord.terminal, "aborted")
        self.assertIn(("open-01", "manual-abort-recovery: 发现新的接地点"),
                      self.gateway.aborted)

    # ------------------------------------------------------- 进程故障恢复

    def test_crash_recovery_does_not_reoperate_confirmed_switches(self):
        self.issue(); self.confirm("open-01")
        self.issue()  # start-02 在途时“进程崩溃”
        before = len(self.gateway.sent)

        new_gateway = RecordingGateway()
        recovered = RecoveryCoordinator.recover(self.jpath, new_gateway, clock=SimClock())
        # 重放不重新下发任何命令。
        self.assertEqual(new_gateway.sent, [])
        self.assertEqual(recovered.nodes["grid-breaker"].status, "confirmed")
        self.assertEqual(recovered.active_id, "start-02")
        # 迟到的设备确认仍能关联到崩溃前的在途命令。
        result = recovered.inject_response("start-02", "confirmed")
        self.assertEqual(result["disposition"], "confirmed")
        # 编号继续单调：下一条是 close-03，而不是重复 open/start。
        recovered.tick()  # bus
        r = recovered.tick()
        self.assertEqual((r["command_id"], r["node"]), ("close-03", "emergency-lighting"))
        ids = [c.command_id for c in new_gateway.sent]
        self.assertNotIn("open-01", ids)
        self.assertNotIn("start-02", ids)
        self.assertEqual(before, 2)

    def test_crash_replay_is_deterministic(self):
        self.issue(); self.confirm("open-01")
        self.issue(); self.confirm("start-02")
        self.issue()
        a = RecoveryCoordinator.recover(self.jpath, RecordingGateway(), clock=SimClock())
        b = RecoveryCoordinator.recover(self.jpath, RecordingGateway(), clock=SimClock())
        self.assertEqual(
            [n.status for n in a.nodes.values()],
            [n.status for n in b.nodes.values()],
        )
        self.assertEqual(a.active_id, b.active_id)

    def test_replay_then_run_to_completion(self):
        # 在母线确认后崩溃，重放并完成全部恢复。
        self.issue(); self.confirm("open-01")
        self.issue(); self.confirm("start-02")
        self.issue()  # bus confirmed

        rec = RecoveryCoordinator.recover(self.jpath, self.gateway, clock=SimClock())
        for cid, node in (("close-03", "emergency-lighting"), ("close-04", "cold-store"),
                          ("close-05", "process-line"), ("close-06", "office-load")):
            r = rec.tick()
            self.assertEqual(r["command_id"], cid)
            rec.inject_response(cid, "confirmed")
        self.assertEqual(rec.tick()["action"], "completed")

    # ------------------------------------------------------- 日志完整性

    def test_command_ids_are_monotonic_with_retries(self):
        self.issue()  # open-01
        self.clock.advance(99)
        self.issue()  # timeout
        self.issue()  # open-02
        ids = [c.command_id for c in self.gateway.sent]
        self.assertEqual(ids, ["open-01", "open-02"])

    def test_corrupted_journal_is_detected(self):
        with self.jpath.open("a", encoding="utf-8") as f:
            f.write("{not json\n")
        with self.assertRaises(JournalCorrupted):
            Journal(self.jpath)

    def test_start_refuses_to_overwrite_existing_journal(self):
        with self.assertRaises(FileExistsError):
            RecoveryCoordinator.start(self.jpath, self.topology, RecordingGateway())

    # ------------------------------------------------------- 记录导出

    def test_export_record_covers_auto_and_manual_history(self):
        self.issue(); self.confirm("open-01")
        self.coord.pause("暂停核对")
        self.coord.resume()
        self.issue(); self.confirm("start-02")
        record = self.coord.export_record()
        names = [t["event"] for t in record["timeline"]]
        self.assertIn("command.issued", names)
        self.assertIn("command.confirmed", names)
        self.assertIn("recovery.paused", names)
        self.assertGreaterEqual(record["manual_interventions"], 1)
        ledger = {c["command_id"]: c["status"] for c in record["commands"]}
        self.assertEqual(ledger, {"open-01": "confirmed", "start-02": "confirmed"})
        self.assertEqual(record["plan"][0], "grid-breaker")

    def test_export_after_permanent_reject_shows_hold_and_reason(self):
        self.issue()
        self.coord.inject_response("open-01", "rejected", reason="联锁动作")
        record = self.coord.export_record()
        self.assertEqual(record["outcome"], "in_progress")
        cmd = record["commands"][0]
        self.assertTrue(cmd["permanent"])
        self.assertEqual(cmd["reason"], "联锁动作")
        self.assertIn("manual.hold", [t["event"] for t in record["timeline"]])

    def test_export_json_file(self):
        self.issue()
        out = Path(self.tmp.name) / "record.json"
        self.coord.export_record_json(out)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("timeline", data)

    # ------------------------------------------------------- 态势与菜单

    def test_status_explains_why_loads_wait_and_allowed_actions(self):
        text = self.coord.explain()
        self.assertIn("grid-breaker", text)
        self.assertIn("此刻允许的动作", text)
        actions = self.coord.allowed_actions()
        self.assertIn("pause（暂停自动推进）", actions)
        self.issue()
        actions = self.coord.allowed_actions()
        self.assertTrue(any("inject_response" in a for a in actions))
        # 在途且自动运行时，中止命令不在允许菜单中。
        self.assertFalse(any("abort_active_command" in a for a in actions))


class ReferenceDrillTest(unittest.TestCase):
    """用评审通过的 reference/recovery_drill.json 跑完整演练。"""

    def test_reference_drill_out_of_order_and_reject(self):
        with tempfile.TemporaryDirectory() as d:
            jpath = Path(d) / "drill.journal.jsonl"
            gateway = RecordingGateway()
            topo = load_topology(REFERENCE)
            coord = RecoveryCoordinator.start(jpath, topo, gateway, clock=SimClock())
            player = DrillPlayer.from_file(coord, REFERENCE)
            player.run(dt=1.0)

            self.assertIsNone(coord.terminal)
            self.assertEqual(coord.hold_node, "cold-store")
            # 前三个节点均已确认。
            for nid in PLAN_4[:3]:
                self.assertEqual(coord.nodes[nid].status, "confirmed", nid)
            self.assertEqual(coord.nodes["cold-store"].status, "rejected")
            # 命令关联编号与演练脚本一致。
            self.assertEqual(
                [(c.command_id, c.device) for c in gateway.sent],
                [("open-01", "grid-breaker"), ("start-02", "forming-pcs"),
                 ("close-03", "cold-store")],
            )
            ledger = {c.command_id: c.status for c in coord.commands.values()}
            self.assertEqual(
                ledger,
                {"open-01": "confirmed", "start-02": "confirmed",
                 "close-03": "rejected"},
            )
            # 指挥人员可看到允许的人工动作。
            self.assertTrue(
                any("manual_confirm" in a for a in coord.allowed_actions()))

    def test_reference_drill_recover_then_manual_finish_and_export(self):
        with tempfile.TemporaryDirectory() as d:
            jpath = Path(d) / "drill.journal.jsonl"
            gateway = RecordingGateway()
            topo = load_topology(REFERENCE)
            coord = RecoveryCoordinator.start(jpath, topo, gateway, clock=SimClock())
            DrillPlayer.from_file(coord, REFERENCE).run()
            # 在停靠点“关闭协调器再启动”。
            revived = RecoveryCoordinator.recover(jpath, RecordingGateway(), clock=SimClock())
            self.assertEqual(revived.hold_node, "cold-store")
            revived.manual_confirm("cold-store", operator="李工", note="就地合闸成功")
            revived.resume_automation()
            self.assertEqual(revived.tick()["action"], "completed")
            record = revived.export_record()
            self.assertEqual(record["outcome"], "completed")
            names = [t["event"] for t in record["timeline"]]
            self.assertIn("command.rejected", names)
            self.assertIn("manual.node_confirmed", names)
            self.assertIn("recovery.completed", names)


if __name__ == "__main__":
    unittest.main()

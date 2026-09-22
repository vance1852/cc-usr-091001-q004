"""指挥人员演练界面：逐条注入响应，查看每个负荷为何等待、能采取什么动作。

用法：
    python -m island_recovery.cli run reference/recovery_drill.json
    python -m island_recovery.cli replay run.journal.jsonl --export record.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .coordinator import RecoveryCoordinator, RecoveryError
from .drill import SimClock
from .gateway import RecordingGateway
from .topology import load_topology

HELP = """\
可用命令（与协调器安全护栏一致）：
  tick [秒]                 推进恢复（可指定经过秒数，用于超时演练）
  confirm <command_id>      注入设备确认
  reject <command_id> [--transient] [原因...]   注入设备拒绝（默认永久拒绝）
  pause / resume            暂停 / 继续自动推进
  suspend / auto            人工接管 / 归还自动化
  abort                     中止在途命令（需先暂停或接管）
  mconfirm <node> [备注...] 现场核实后人工确认节点（拓扑前置仍强制）
  grid                      报告市电提前返回（受控降级）
  stop <原因...>            人工终止整个恢复
  status                    重新打印态势
  export [路径]             导出完整恢复记录 JSON
  help / quit
"""


def _print_status(coord: RecoveryCoordinator) -> None:
    print(coord.explain())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="园区孤网恢复协调演练台")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="按拓扑启动一次新恢复")
    p_run.add_argument("topology", type=Path)
    p_run.add_argument("--journal", type=Path, default=Path("recovery.journal.jsonl"))
    p_run.add_argument("--auto", action="store_true",
                       help="启动后立即推进第一步")

    p_replay = sub.add_parser("replay", help="从日志重放恢复（进程故障后恢复）")
    p_replay.add_argument("journal", type=Path)
    p_replay.add_argument("--export", type=Path, default=None,
                          help="重放后直接导出恢复记录")
    args = parser.parse_args(argv)

    gateway = RecordingGateway()
    clock = SimClock()
    if args.cmd == "run":
        topology = load_topology(args.topology)
        if args.journal.exists():
            print(f"日志 {args.journal} 已存在；如需接续请使用 replay，"
                  "或指定新的 --journal 路径。", file=sys.stderr)
            return 2
        coord = RecoveryCoordinator.start(args.journal, topology, gateway, clock=clock)
        print(f"已建立恢复日志：{args.journal}")
        if args.auto:
            print(coord.tick())
    else:
        coord = RecoveryCoordinator.recover(args.journal, gateway, clock=clock)
        print(f"已从日志重放恢复：{args.journal}（重放不重复下发任何命令）")
        if args.export:
            path = coord.export_record_json(args.export)
            print(f"恢复记录已导出：{path}")
            return 0

    _print_status(coord)
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        cmd, args_left = parts[0], parts[1:]
        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(HELP)
            elif cmd == "status":
                pass
            elif cmd == "tick":
                if args_left:
                    clock.advance(float(args_left[0]))
                result = coord.tick()
                print(json.dumps(result, ensure_ascii=False))
            elif cmd == "confirm" and len(args_left) == 1:
                print(json.dumps(coord.inject_response(args_left[0], "confirmed"),
                                 ensure_ascii=False))
            elif cmd == "reject" and len(args_left) >= 1:
                cid = args_left[0]
                transient = "--transient" in args_left
                rest = [a for a in args_left[1:] if a != "--transient"]
                print(json.dumps(coord.inject_response(
                    cid, "rejected", permanent=not transient, reason=" ".join(rest),
                ), ensure_ascii=False))
            elif cmd == "pause":
                coord.pause("指挥人员暂停")
            elif cmd == "resume":
                coord.resume()
            elif cmd == "suspend":
                coord.suspend_automation("指挥人员接管")
            elif cmd == "auto":
                coord.resume_automation()
            elif cmd == "abort":
                coord.abort_active_command("指挥人员中止")
            elif cmd == "mconfirm" and args_left:
                coord.manual_confirm(args_left[0], operator="operator",
                                     note=" ".join(args_left[1:]))
            elif cmd == "grid":
                coord.grid_returned()
            elif cmd == "stop":
                coord.manual_abort(" ".join(args_left) or "指挥人员终止")
            elif cmd == "export":
                path = Path(args_left[0]) if args_left else Path("recovery.record.json")
                coord.export_record_json(path)
                print(f"恢复记录已导出：{path}")
            else:
                print("无法识别的命令或参数不足，输入 help 查看菜单。", file=sys.stderr)
                continue
        except RecoveryError as exc:
            print(f"安全护栏拦截：{exc}", file=sys.stderr)
        _print_status(coord)
        if coord.terminal:
            print("恢复已终结。可 export 导出记录后退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

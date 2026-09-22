"""演练命令行：逐条注入设备响应，观察负荷等待原因与当前允许动作。

用法：
    python -m campus_island_recovery DRILL.json --journal run.jsonl [--auto]
    python -m campus_island_recovery DRILL.json --journal run.jsonl --resume
    python -m campus_island_recovery DRILL.json --journal run.jsonl --export record.json

交互命令：inject / status / why <节点> / pause / resume / takeover <姓名> /
release <姓名> / retry <节点> / mark <节点> / utility / abort <姓名> /
export [路径] / help / quit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .drill import DrillRunner
from .report import export_record, render_record_text, render_status

HELP = """命令：
  inject            注入下一条既有设备响应
  status            查看各节点状态、等待原因与允许动作
  why <节点>        查看该负荷/设备为何仍在等待
  pause / resume    暂停 / 恢复自动推进
  takeover <姓名>   人工接管；release <姓名> 交还自动
  retry <节点>      对被拒绝/结果未知的设备重新下令
  mark <节点>       现场确认设备已操作完成
  utility           模拟市电提前返回（受控降级）
  abort <姓名>      中止整个恢复过程
  export [路径]     导出完整恢复记录（JSON 与文本）
  quit              退出演练
"""


def _print_result(result: dict) -> None:
    mark = "✓" if result.get("ok") else "✗"
    print(f"{mark} {result.get('outcome')}: {result.get('detail')}")


def _do_export(runner: DrillRunner, path: str) -> None:
    record = runner.coordinator.export_record()
    export_record(record, path)
    text_path = str(Path(path).with_suffix(".txt"))
    Path(text_path).write_text(render_record_text(record), encoding="utf-8")
    print(f"已导出 {path} 与 {text_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="campus_island_recovery", description="园区孤网恢复协调演练"
    )
    parser.add_argument("drill", help="演练 JSON（供电拓扑 + 设备响应序列）")
    parser.add_argument("--journal", default="recovery_journal.jsonl", help="事件 journal 路径")
    parser.add_argument("--timeout", type=float, default=30.0, help="设备命令超时秒数")
    parser.add_argument("--auto", action="store_true", help="自动逐条注入全部响应后导出")
    parser.add_argument("--resume", action="store_true", help="从既有 journal 恢复协调器")
    parser.add_argument("--export", dest="export_path", default=None, help="结束时导出记录路径")
    args = parser.parse_args(argv)

    journal_path = Path(args.journal)
    if args.resume:
        runner = DrillRunner.resume(args.drill, journal_path, command_timeout=args.timeout)
        print("已从 journal 恢复协调器（已确认的开关不会再次操作）")
    else:
        if journal_path.exists():
            journal_path.unlink()
        runner = DrillRunner(args.drill, journal_path, command_timeout=args.timeout)

    if args.auto:
        try:
            for result in runner.run_all():
                _print_result(result)
            print(render_status(runner.coordinator.status()))
            if args.export_path:
                _do_export(runner, args.export_path)
        finally:
            runner.journal.close()
        return 0

    print(render_status(runner.coordinator.status()))
    print(HELP)
    while True:
        try:
            line = input("drill> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        verb, *rest = line.split()
        arg = " ".join(rest)
        try:
            if verb == "inject":
                _print_result(runner.inject_next())
                print(render_status(runner.coordinator.status()))
            elif verb == "status":
                print(render_status(runner.coordinator.status()))
            elif verb == "why":
                for reason in runner.coordinator.waiting_reasons(arg):
                    print(f"  {arg}: {reason}")
            elif verb == "pause":
                _print_result(runner.coordinator.pause())
            elif verb == "resume":
                _print_result(runner.coordinator.resume())
            elif verb == "takeover":
                _print_result(runner.coordinator.takeover(arg or "指挥员"))
            elif verb == "release":
                _print_result(runner.coordinator.release_to_auto(arg or "指挥员"))
            elif verb == "retry":
                _print_result(runner.coordinator.manual_retry(arg, operator="指挥员"))
            elif verb == "mark":
                _print_result(runner.coordinator.manual_mark(arg, operator="指挥员", rationale="演练现场确认"))
            elif verb == "utility":
                _print_result(runner.coordinator.utility_returned())
            elif verb == "abort":
                _print_result(runner.coordinator.abort(operator=arg or "指挥员"))
            elif verb == "export":
                _do_export(runner, arg or args.export_path or "recovery_record.json")
            elif verb in ("quit", "exit"):
                break
            elif verb == "help":
                print(HELP)
            else:
                print(f"未知命令 {verb!r}，输入 help 查看用法")
        except Exception as exc:  # 演练 CLI：任何错误都不应打断会话
            print(f"操作失败：{exc}")
    if args.export_path:
        _do_export(runner, args.export_path)
    runner.journal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""恢复记录导出与状态展示。

记录包含自动决策（命令下达及其前置依据）、设备响应、超时/拒绝/中止、
控制模式切换与全部人工处置，可按 journal 完整重放。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MANUAL_EVENT_TYPES = ("manual_mark", "manual_retry", "control_changed")


def build_record(coordinator) -> dict[str, Any]:
    events = list(coordinator.journal.events)
    counts: dict[str, int] = {}
    for ev in events:
        counts[ev["type"]] = counts.get(ev["type"], 0) + 1
    return {
        "schema": "campus-island-recovery/record@1",
        "topology": [
            {
                "id": n.id,
                "kind": n.kind,
                "requires": list(n.requires),
                "priority": n.priority,
            }
            for n in coordinator.topology
        ],
        "final_status": coordinator.status(),
        "event_counts": counts,
        "manual_interventions": [
            e for e in events if e["type"] in MANUAL_EVENT_TYPES
        ],
        "events": events,
    }


def export_record(record: dict[str, Any], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _fmt_ts(ts) -> str:
    if ts is None:
        return "T+-----"
    if abs(ts) < 10_000_000:  # 虚拟时钟：相对秒
        return f"T+{ts:6.1f}"
    return f"{ts:.3f}"


def render_status(status: dict[str, Any]) -> str:
    lines = [
        f"控制模式 {status['control']} / 阶段 {status['phase']} / 时刻 {_fmt_ts(status['now'])}",
    ]
    for node in status["nodes"]:
        line = f"  {node['id']:<14} {node['kind']:<10} {node['state']:<10}"
        if node.get("active_command"):
            line += f" [{node['active_command']}]"
        if node.get("ready_via") and node["state"] == "ready":
            line += f" (via {node['ready_via']})"
        lines.append(line)
        for reason in node["waiting"]:
            lines.append(f"      └─ {reason}")
    lines.append("允许动作：" + "、".join(status["allowed_actions"]))
    return "\n".join(lines)


def render_record_text(record: dict[str, Any]) -> str:
    lines = ["=== 孤网恢复记录 ===", ""]
    for ev in record["events"]:
        seq = ev.get("seq", "?")
        ts = _fmt_ts(ev.get("ts"))
        body = " ".join(
            f"{k}={v}" for k, v in ev.items() if k not in ("seq", "ts", "type")
        )
        lines.append(f"#{seq:<4} {ts} {ev['type']:<20} {body}".rstrip())
    lines += ["", "=== 最终状态 ===", render_status(record["final_status"])]
    counts = record.get("event_counts", {})
    if counts:
        lines.append("")
        lines.append(
            "事件统计：" + "、".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        )
    return "\n".join(lines)

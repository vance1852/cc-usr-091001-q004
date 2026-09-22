# 园区孤网恢复协调

雷暴后恢复复盘的结论是：柴油机、构网型储能与关键产线被同时下令启动，
设备互锁把送电过程卡死。本仓库把评审通过的安全原则固化为一套可暂停、
可接管、可恢复的 Python 协调系统：

> **先确认并网点隔离 → 再取得稳定电源与母线条件 → 最后按负荷优先级送电。**

`reference/recovery_drill.json` 保存评审通过的供电拓扑与一次黑启动演练的
设备响应（含乱序确认与设备拒绝）；`tests/fixtures/campus_topology.json`
是包含柴油发电机、构网储能、关键母线与四级负荷的扩展演练拓扑。

## 架构

| 模块 | 职责 |
|---|---|
| `island_recovery/topology.py` | 拓扑载入与校验：引用闭合、无环、阶段结构（电源必在隔离后、母线必在电源后、负荷必在母线后）、负荷优先级；产出确定性恢复计划 |
| `island_recovery/journal.py` | append-only JSONL 事件日志（`O_APPEND`+`fsync`），序号连续校验，状态全部可重放 |
| `island_recovery/coordinator.py` | 状态机核心：单一推进入口、命令台账、暂停/接管/停靠、超时与中止、市电返回受控降级、态势解释与记录导出 |
| `island_recovery/gateway.py` | 设备命令唯一出口（`send`/`abort` 可接真实设备协议），`RecordingGateway` 用于演练 |
| `island_recovery/drill.py` | 模拟时钟 + 按 `command_id` 关联的脚本响应播放器，自动跑到完成/中止/人工停靠 |
| `island_recovery/cli.py` | 指挥人员演练台：逐条注入响应、查看等待原因与允许动作、重放与导出 |

### 安全机制如何落实

- **拓扑门禁不可绕过**：每一步都重新校验 `requires`；母线是"条件节点"
  无开关命令，上游电源稳定才确认带电；负荷严格按 `priority` 升序。
- **柴油机互锁消除在设计阶段**：`restorable: false` 的节点（柴油发电机）
  保留在拓扑图中供引用，但**永不进入自动恢复计划**，协调器没有任何代码
  路径会对它下令；自动节点若依赖它，拓扑载入即报错。
- **命令全关联**：`open-01 / start-02 / close-03 …` 由协调器生成且单调
  不重用。发出→确认/拒绝/超时/中止全部带编号落盘。同一时刻只有一条
  在途命令；未知编号、错设备、重复确认、迟到回复分别记为
  `uncorrelated / device_mismatch / duplicate / late`，**只入账不推进**。
- **先落盘再出口**：命令先写日志再调用网关。崩溃发生在下发前则命令
  不存在；发生在落盘后命令保持"在途"，重放绝不重复操作已确认开关，
  迟到确认仍可关联到旧编号。
- **永久拒绝停在人工处置位置**：自动挂起自动化（`manual.hold`，非终态），
  停靠点未解除前不允许归还自动化；现场核实后 `manual_confirm`（拓扑前置
  同样强制）再继续。暂态拒绝与超时在限定次数内以**新编号**重发。
- **市电提前返回受控降级**：报告后下一推进步立即中止在途命令并以
  `aborted` 收尾，转人工执行市电同期/回切，暂停状态也不例外。
- **人工动作同样受拓扑约束**：暂停或接管后才能中止命令/人工确认；
  上游未确认的人工确认一律拦截。

## 快速开始

```bash
python3 -m unittest discover -s tests -v   # 43 个场景测试
```

交互演练（设备响应由指挥逐条注入）：

```bash
PYTHONPATH=. python3 -m island_recovery.cli run reference/recovery_drill.json \
    --journal /tmp/recovery.journal.jsonl
# tick → confirm open-01 → tick → confirm start-02 → tick → tick
# → reject close-03 机构卡涩（永久拒绝，进入人工处置停靠点）
# → quit（模拟协调器中途关闭）

PYTHONPATH=. python3 -m island_recovery.cli replay /tmp/recovery.journal.jsonl
# mconfirm cold-store 就地检查正常 → auto → tick（完成）
# → export /tmp/record.json
```

脚本化跑评审演练（含拒绝场景，最终停在人工处置位置）：

```python
from island_recovery import Journal, RecordingGateway, RecoveryCoordinator
from island_recovery import load_topology
from island_recovery.drill import DrillPlayer, SimClock

topo = load_topology("reference/recovery_drill.json")
coord = RecoveryCoordinator.start(
    "/tmp/drill.journal.jsonl", topo, RecordingGateway(), clock=SimClock())
DrillPlayer.from_file(coord, "reference/recovery_drill.json").run()
print(coord.explain())          # 每个未送电负荷为何等待、此刻允许什么动作
```

进程故障后接续：`RecoveryCoordinator.recover(journal_path, gateway)`。
恢复记录：`coord.export_record()` / `export_record_json(path)`，包含
完整时间线（标注自动决策 `auto` 与人工介入 `manual`）、命令台账与最终
节点状态。

## 运行环境

Python 3.11 及以上，标准库实现，无第三方依赖。

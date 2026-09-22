# 园区孤网恢复协调

仓库保存园区供电拓扑和一次黑启动演练的设备响应。节点之间的 `requires` 表示送电前必须稳定的上游，隔离开关确认后才能启动构网型储能，普通负荷不得早于关键母线恢复。

`reference/recovery_drill.json` 中既有乱序确认，也有设备拒绝。命令编号由协调系统生成，设备返回的关联编号用于识别迟到响应和重复响应。

运行环境采用 Python 3.11 及以上版本。执行 `python -m unittest discover -s tests -v` 检查拓扑引用和演练数据。

## 孤网恢复协调系统（campus_island_recovery）

把评审通过的安全原则落实为可暂停、可接管、可恢复的执行过程：

1. **拓扑前置约束**：并网点隔离确认 → 稳定电源与母线条件 → 按负荷优先级送电。任何自动或人工动作都不得越过 `requires`。
2. **命令全生命周期关联**：命令编号由协调器生成且不复用；确认、超时、拒绝、中止全部按关联编号入账。乱序、迟到、重复的响应只记录，不误推进下一步。
3. **永久拒绝即停**：设备拒绝后停在该节点，等待人工处置（重试 / 现场确认 / 中止），不擅自绕过。
4. **市电提前返回**：进入受控降级——在途孤网命令中止、不再下达新命令、已确认状态保持，等待并网规程。
5. **崩溃可恢复**：全部状态变迁先落 journal 再生效。进程重启后已确认的开关绝不再次操作；在途命令转为"结果未知"，等待人工补录响应或显式重试。

### 演练

```bash
# 交互式：逐条注入既有响应，随时查看负荷为何等待、此刻允许哪些动作
python -m campus_island_recovery reference/recovery_drill.json --journal run.jsonl

# 自动注入全部响应并导出恢复记录（JSON + 文本）
python -m campus_island_recovery reference/recovery_drill.json --journal run.jsonl --auto --export record.json

# 模拟进程崩溃后恢复：--resume 从既有 journal 重建协调器
python -m campus_island_recovery reference/recovery_drill.json --journal run.jsonl --resume
```

交互命令：`inject`（注入下一条响应）、`status`、`why <节点>`、`pause` / `resume`、
`takeover <姓名>` / `release <姓名>`、`retry <节点>`、`mark <节点>`、
`utility`（市电返回）、`abort`、`export`、`quit`。

### 模块

| 模块 | 职责 |
| --- | --- |
| `topology.py` | 评审通过的供电拓扑：节点类型、`requires` 前置、送电优先级 |
| `protocol.py` | 设备握手协议：命令生命周期与关联编号 |
| `journal.py` | 追加式事件 journal，崩溃恢复与记录导出的事实来源 |
| `coordinator.py` | 协调器状态机：自动推进、暂停/接管、超时、降级、人工处置 |
| `drill.py` | 演练执行器：按 delay 推进虚拟时钟，逐条注入既有响应 |
| `report.py` | 恢复记录导出与状态展示 |
| `cli.py` | 演练命令行 |

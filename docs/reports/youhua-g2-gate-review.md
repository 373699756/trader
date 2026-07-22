# youhua G2 阶段 2 门禁复核记录

状态：G2 已发布；A 已收到 A2/B2/C2/D2 阶段 2 交接材料，四方均为 `ready_for_gate=yes`。
本批只发布共同门禁，不启动 A3。

## 1. 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex A / G2 gate review |
| start_head | `9477514784ff3a2da0e7fe27ddd37e8d03f097a5` |
| upstream | `origin/branch` |
| upstream_commit | `9477514784ff3a2da0e7fe27ddd37e8d03f097a5` |
| CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| A2_commit | `9477514784ff3a2da0e7fe27ddd37e8d03f097a5` |
| B/C/D 内部算法 | 未执行、未修改 |

## 2. 阶段 2 交接材料

| Codex | 报告路径 | 报告 base | gate 状态 | A 复核结论 |
| --- | --- | --- | --- | --- |
| A | `docs/reports/youhua-a2-public-skeleton.md` | `646509619bd491b9333f3361bfe6cda8bbf9ee96` -> `9477514784ff3a2da0e7fe27ddd37e8d03f097a5` | yes | A2 公共骨架、替身、双层内存契约已发布 |
| B | `tests/fixtures/market_data/youhua_b2/report_to_a.md` | `03c710ee854dbead3446c0f0400ed7fe154e87b6` | yes | B2 已补齐 A2 public envelope 适配，自报 B 单域、component、type-check 和性能验证通过 |
| C | `tests/fixtures/deepseek/youhua_c2/report_to_a.md` | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` | yes | C2 已补齐标准 `ready_for_gate: yes` 字段，自报 DeepSeek V4、long 隔离、预算和缓存单域验证通过 |
| D | `docs/reports/youhua-d1-p6-web.md` 第 9 节 | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` | yes | D2 自报 P6/SSE/DOM 单域验证通过，仍有非 D 全局阻断 |

## 3. G2 判定

`docs/plan_youhua.md` 规定 G2 必须满足：

- A2、B2、C2、D2 单域测试分别通过；
- B/C/D 标准交接包都基于同一个 `CONTRACT_BASE`；
- 各自 diff 不越界；
- 三方不得创建第二套公共 schema；
- G2 通过后才进入 A3。

当前发布 G2，证据如下：

1. A2 报告记录公共骨架、替身和双层内存契约已完成，`ready_for_gate=yes`。
2. B2 报告已从 `ready_for_gate=no` 更新为 `ready_for_gate=yes`，并记录 A2 public
   `MarketChangeSet`、`FeatureSnapshotEnvelope` 和 `P4ConsumerStub` 适配。
3. C2 报告已补齐标准 `ready_for_gate: yes` 字段。
4. D2 报告位于 D1 报告追加章节中，记录 `ready_for_gate yes`。

## 4. A 后续动作

- B2 标准字段已补齐；后续若 B 修改公共 schema 请求，仍必须走 A amendment。
- C2 标准字段已补齐；后续若 C 修改公共 schema 请求，仍必须走 A amendment。
- D2 报告已接收；若 D 后续修改公共 schema 请求，仍必须走 A amendment。
- A3 必须等待下一次用户继续指令；本批不合并 B/C/D 实现、不连接真实实现、不修改生产默认。

## 5. 本批验证

- 仅读取 B2/C2/D2 报告和 fixture 路径，未执行 B/C/D 内部算法。
- `git diff --check` 通过。

## 6. 2026-07-23 复核

| 项 | 值 |
| --- | --- |
| recheck_head | `03c710ee854dbead3446c0f0400ed7fe154e87b6` |
| C2 标准字段 | 已补齐，`ready_for_gate: yes` |
| B2 标准字段 | 仍为 `ready_for_gate=no` |
| G2 判定 | 未发布 |

本次复核只更新 C2 标准字段接收状态和 G2 阻塞原因；A 未执行 B/C/D 内部算法，未合并真实
实现，未开始 A3。

ready_for_gate: `no; G2 is blocked by B2 ready_for_gate=no`

## 7. 2026-07-23 再复核与 G2 发布

| 项 | 值 |
| --- | --- |
| recheck_head | `ac90f61ece2a5966293de1649d2b467d88cc48fa` |
| A2 标准字段 | `ready_for_gate=yes` |
| B2 标准字段 | 已补齐，`ready_for_gate=yes` |
| C2 标准字段 | 已补齐，`ready_for_gate=yes` |
| D2 标准字段 | 已补齐，`ready_for_gate=yes` |
| G2 判定 | 已发布 |

G2 发布只表示阶段 2 四方交接材料和单域验证证据已收齐。A 尚未开始 A3，尚未合并 B/C/D
生产实现，尚未连接真实实现，尚未修改生产默认。

ready_for_gate: `yes; G2 is published and A3 has not started`

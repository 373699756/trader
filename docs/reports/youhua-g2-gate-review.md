# youhua G2 阶段 2 门禁复核记录

状态：G2 未发布；A 已收到 B2/C2/D2 阶段 2 交接材料，但共同门禁未满足，A 不进入 A3。

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
| B | `tests/fixtures/market_data/youhua_b2/report_to_a.md` | `646509619bd491b9333f3361bfe6cda8bbf9ee96` | no | B2 自报实现和 B 单域检查基本就绪，但共享工作树 component/type gates 仍被非 B 漂移阻断 |
| C | `tests/fixtures/deepseek/youhua_c2/report_to_a.md` | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` | ready for G2 review | C2 自报 DeepSeek V4、long 隔离、预算和缓存单域验证通过 |
| D | `docs/reports/youhua-d1-p6-web.md` 第 9 节 | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` | yes | D2 自报 P6/SSE/DOM 单域验证通过，仍有非 D 全局阻断 |

## 3. G2 判定

`docs/plan_youhua.md` 规定 G2 必须满足：

- A2、B2、C2、D2 单域测试分别通过；
- B/C/D 标准交接包都基于同一个 `CONTRACT_BASE`；
- 各自 diff 不越界；
- 三方不得创建第二套公共 schema；
- G2 通过后才进入 A3。

当前不发布 G2，原因如下：

1. B2 报告的 `ready_for_gate` 为 `no`，不满足共同门禁。
2. B2 报告明确仍有 shared-worktree component/type gates 阻断；A 不能用主观判断替代 B 的
   自报门禁状态。
3. C2 报告使用“ready for G2 review”措辞，但未采用标准 `ready_for_gate: yes/no` 字段；
   A 暂按“可进入复核”记录，不把它升级为 G2 通过证据。
4. D2 报告位于 D1 报告追加章节中，记录 `ready_for_gate yes`，但 G2 仍受 B2 阻断。

## 4. A 后续动作

- 等待 B 提交 `ready_for_gate=yes` 的 B2 标准交接包，且说明 A2 后公共契约适配状态。
- 等待 C 如需补齐标准字段时提交 `ready_for_gate=yes` 明确报告；若 C 保持现有报告，A 在
  下次 G2 复核中仍会把它列为需人工确认项。
- D2 报告已接收；若 D 后续修改公共 schema 请求，仍必须走 A amendment。
- G2 发布前，A 不合并 B/C/D 实现、不连接真实实现、不修改生产默认、不开始 A3。

## 5. 本批验证

- 仅读取 B2/C2/D2 报告和 fixture 路径，未执行 B/C/D 内部算法。
- `git diff --check` 通过。

ready_for_gate: `no; G2 is blocked by B2 ready_for_gate=no and C2 missing the standard ready_for_gate field`

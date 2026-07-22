# youhua G3 阶段 3 门禁复核记录

状态：G3 已发布；A 已完成 A3 集成 handoff，并已收到 B3/C3/D3 基于 A3 集成提交的
`ready_for_gate=yes` 专业复验报告。本批只发布阶段 3 共同门禁，不启动 A4。

## 1. 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex A / G3 gate review |
| start_head | `812de4390d42213c684b4e2096453c4775903443` |
| upstream | `origin/branch` |
| upstream_commit | `812de4390d42213c684b4e2096453c4775903443` |
| CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| A3_commit | `812de4390d42213c684b4e2096453c4775903443` |
| start_worktree | clean |
| B/C/D 内部算法 | A 仅集成已交接补丁与报告，未替代专业域内部算法 |

## 2. 阶段 3 交接材料

| Codex | 预期报告 | 当前材料 | gate 状态 | A 复核结论 |
| --- | --- | --- | --- | --- |
| A | `docs/reports/youhua-a3-integration.md` | 已提交，基于 `812de4390d42213c684b4e2096453c4775903443` | yes | A3 集成 handoff 已发布，明确 G3 等待 B3/C3/D3 |
| B | B3 集成态等价/性能报告 | `tests/fixtures/market_data/youhua_b3/report_to_a.md` | yes | B3 自报 scalar/columnar 等价、change set 全量一致、component/type-check、5500 行/360 候选/100 tick 和 RSS 验证通过 |
| C | C3 集成态请求/降级报告 | `tests/fixtures/deepseek/youhua_c3/report_to_a.md` | yes | C3 自报跨三策略 raw facts 单请求、普通 quote-only 零新增 HTTP、long/late/budget/all-fail 降级通过；C3.1 修复 C-owned raw facts cache identity |
| D | D3 集成态 P6/Web 报告 | `docs/reports/youhua-d1-p6-web.md` 第 10 节 | yes | D3 自报 P4/P5 差量重发布、P6 热读/single-flight、SSE patch/游标/慢客户端/冻结/overlay/ETag、桌面视口和 P6 原子替换峰值验证通过 |

## 3. G3 判定

`docs/plan_youhua.md` 规定 G3 必须满足：

- source -> P6 -> Web 全链可运行；
- DeepSeek 可用和不可用两条链都产生正确完整投影；
- 四个 Codex 的集成态专业报告均通过；
- 所有已知集成问题已归属 owner，没有未解释额外 HTTP、热读或重复扣分。

当前发布 G3，证据如下：

1. A3 报告记录 source -> P6 -> Web 集成 handoff 已完成，且全链接线、身份收敛和集成测试已执行。
2. B3 报告记录 scalar/columnar 等价、change set 一致、性能/内存和 `ready_for_gate=yes`。
3. C3 报告记录 DeepSeek 可用/不可用、raw facts 缓存、价格变化路由、预算/失败降级和 `ready_for_gate=yes`。
4. D3 报告记录 P6/Web 热读、差量 patch、SSE、冻结、overlay、ETag、桌面视口、P6 原子替换峰值和 `ready_for_gate=yes`。
5. A 复核到的已知集成问题均已由 owner 报告归属，当前无未解释额外 HTTP、热读或重复扣分证据。

## 4. 后续动作边界

- 阶段 3 完成；A4.x 必须等待下一次用户继续指令。
- 本批不启动 A4、不扩大验收范围、不创建 PR、tag 或 release。
- 后续 A4 仍需执行跨域正确性、故障注入、完整质量、兼容、性能总门禁和问题闭环。

## 5. 本批验证

- 读取阶段 3 计划、A3 报告、B3 报告、C3 报告和 D3 报告；A 未替代 B/C/D 专业实现逻辑。
- `tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py` 覆盖 G3
  发布状态和 docs 报告白名单。
- `make format-check` 通过。
- `make lint` 通过。
- `make type-check` 通过：162 个源码文件。
- `make test` 通过。
- `make package` 首次在沙箱内因构建依赖网络受限失败；提升权限后通过，生成物已清理。
- 仓库外 wheel 安装到 `/tmp/trader-wheel-g3` 后可导入 `trader`、读取 `index.html` 与
  `dashboard.js` 包资源，并可执行 `trader.entrypoints.cli --help`。

ready_for_gate: `yes; G3 is published and A4 has not started`

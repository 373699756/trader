# Youhua G5 最终共同门禁

## 1. 发布结论

状态：`PASS`。G5 已发布，`docs/plan_youhua.md` 的阶段 1-5 与 G1-G5 至此全部闭合。

```text
gate: G5
start_head: ff658a5f961f1dccf4a7b1d1a84c69666801490c
start_upstream: origin/branch / ff658a5f961f1dccf4a7b1d1a84c69666801490c
contract_base: 45bd2fab992d36eb873b7c448fbd9739f0cad43c
verdict: PASS
ready_for_gate: yes
final_git_check: HEAD == @{upstream}
terminal_state: G5 is published and plan_youhua is complete
```

本批只发布 G5 最终共同门禁，不修改产品、策略、schema、配置、迁移、运行代码或 Web
资源，不进入 `docs/plan.md` 的后续工程章节。

## 2. G5 四项完成条件

| 完成条件 | 结论 | 证据 |
| --- | --- | --- |
| B/C/D 均明确签字通过 | PASS | B5、C5、D5 均为 `PASS / ready_for_gate=yes`，且均无公共接口申请或已知未解决 owner 缺陷。 |
| A4 全部门禁仍通过 | PASS | G5 重新执行 A4 内存压力、固定 v17 16 项性能、B4 列式、v16 三板评分、全仓门禁、桌面和 wheel 验收。 |
| 文档、代码、测试、配置和 CHANGELOG 一致 | PASS | 两份权威文档、`pyproject.toml`、runtime 配置、A5 报告及活动实现的版本、预算、内存、冻结和 API 契约一致；G5 只增加报告、交付契约和 CHANGELOG。 |
| 最终只有一个交付 commit 且上游一致 | PASS | 仓库规则禁止 squash 已推送历史；G5 批次只创建一个新的交付 commit，提交后立即推送并分别读取本地与上游哈希确认一致。 |

## 3. B5/C5/D5 最终签字

| Owner | 报告 | 状态 | 核心结论 |
| --- | --- | --- | --- |
| B5 | `tests/fixtures/market_data/youhua_b5/report_to_a.md` | `PASS / ready_for_gate=yes` | P1-P3、dirty、列式等价、性能和内存通过；完整实时列式窄路径保持 Eastmoney/Sina 边界。 |
| C5 | `tests/fixtures/deepseek/youhua_c5/report_to_a.md` | `PASS / ready_for_gate=yes` | V4/Pro schema、证据路由、缓存、预算和本地保守映射通过；无新增物理请求路径。 |
| D5 | `docs/reports/youhua-d1-p6-web.md` 第 12 节 | `PASS / ready_for_gate=yes` | P6、持久化分流、SSE、DOM、游标和 resync 通过；current/resident 热读不访问持久化。 |

A5 已在 `ff658a5` 收齐并复核三份签字，完成完整 diff Review、文档闭合、单批提交与
`HEAD == @{upstream}`；G5 以该已推送提交为干净基线，不重复承载 C5 或 D5 代码。

## 4. A4 与最终门禁复验

### 4.1 固定性能和内存

- A4 同进程压力：逻辑缓存 `205,468,511 <= 260,046,848` 字节，峰值 RSS
  `385,851,392 <= 402,653,184` 字节，结束 USS `356,020,224` 字节；scalar/columnar
  业务 SHA-256 均为
  `af791c795eb6447976b0542986c60bf85f229d771146d2b046a4abbcac9436e3`。六池约 70%、
  8 股 DeepSeek、20 日 P6、cold prefetch、12 次原子替换和 32 个慢客户端均实际并存。
- 正式 `perf-check --suite all`：16 项绝对指标全部通过、`absolute_failures={}`、
  `relative_failures={}`、100 tick 分配增长 `0.0%`、外部网络调用 `0`。
- B4 固定身份首轮业务哈希、绝对时延和内存全部通过，但共享宿主下列式相对改善
  `16.842%`，低于 20%。无并行负载原样重跑后 scalar/columnar process CPU P95 为
  `1868.570/1366.784 ms`，改善 `26.854%` 并通过；绝对标准化/合并/canonical P95 为
  `196.075/754.937/1205.732 ms`，逻辑缓存 `29,661,328` 字节、峰值 RSS
  `288,395,264` 字节、增长 `0.0%`。两次业务哈希一致，未通过修改代码“修正”计时结果。
- v16 三板：单板预选 `28.421 ms`、单板评分 `4.016 ms`、三板三策略墙钟
  `309.513 ms`、全局选择 `3.097 ms`，四项均低于固定绝对上限。并行/顺序比 P95
  `0.883`，因此只宣称绝对预算和失败域隔离，不宣称 CPU 加速。

### 4.2 质量、可安装性和桌面

- `make format-check`、`make lint`、`make type-check`、`make test`、`make package`
  全部通过；架构 AST、`create_app()` 无副作用、固定融合 `83.40`、188 原子预算、
  冻结恢复、哈希一致性、SSE 游标/慢客户端均包含在最终回归。
- 仓库外全新环境安装 wheel 后，从 `site-packages` 导入 `trader`，执行
  `trader-cli --help` 与 `validate-config`，读取模板、4 CSS、2 JavaScript、2 SVG 共
  9 项资源，并通过 `pip check`。
- Firefox 152.0.4 在 1280x720、1440x900、1920x1080 精确内容视口均显示 18 行，无
  白屏、页面级横向溢出、关键重叠或浏览器错误，详情抽屉三分区位于视口内；两次有效
  patch 为零完整推荐 GET、成功应用 2 次。

## 5. 文档、代码、测试、配置与提交一致性

- 产品/架构/时间线/API/运维以 `docs/software-business-design.md` 为唯一权威；策略以
  `docs/recommendation-strategy.md` 为唯一权威；依赖和入口以 `pyproject.toml` 为唯一权威。
- runtime 继续固定 `cache_logical_bytes=260046848`、
  `process_peak_rss_bytes=402653184`、DeepSeek 全局物理上限 188、SSE history 256 和
  每客户端队列 16；G5 没有调整这些值。
- `deepseek_v4_review_facts_v1`、P3/P4/P5/P6 公共接缝、Web envelope/patch 版本、固定
  融合、冻结边界和 long 语义均未变化。
- 本报告是阶段性交付证据，不形成新的产品或策略定义；本批测试只保证 G5 必须在
  B5/C5/D5、A4 和 A5 全部完成后才能发布。

`docs/plan_youhua.md` 的“最终只有一个交付 commit”与仓库级 `AGENTS.md` 的逐批单提交、
禁止 squash/amend/改写历史共同适用：保留已经推送的 A/B/C/D/G1-G4 审计提交；本次
G5 只新增一个 Conventional Commit，不 force-push、不创建 PR、tag 或 release。

## 6. 剩余外部风险与停止条件

当前没有已知未解决的仓库内 G5 缺陷。以下不被固定离线门禁伪装为已解决：

- 本机只实际运行 Python 3.14.4；Python 3.10-3.13 由 Ruff/mypy/wheel 元数据静态覆盖；
- 真实行情源和 DeepSeek 时延、可用性及数据质量仍是外部运行风险，本地降级已有回归；
- B4 相对 CPU 指标受共享宿主调度影响，必须保留固定身份、绝对预算和重复测量上下文；
- Firefox SWGL 与 fixture model catalog warning 未形成 DOM、SSE 或测试失败；
- 结构、可靠性与性能证据不证明投资收益，真实前瞻收益验证仍是独立后续事项。

提交后必须推送当前跟踪分支，并分别读取 `HEAD` 与 `@{upstream}`；只有哈希相同才完成
G5。本批完成后停止，等待用户下一条指令。

```text
G5 is published and plan_youhua is complete
```

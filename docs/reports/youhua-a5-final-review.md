# Youhua A5 最终 Review

## 1. 结论与边界

状态：`PASS`。本批完整执行 A5.1-A5.5，只闭合阶段 5 的 Codex A 章节，不发布相邻的 G5。

```text
codex_and_phase: Codex A / A5.1-A5.5
start_head: cd443eaf64e1ab6640a7ac7ccaca077c9a898edb
start_upstream: origin/branch / cd443eaf64e1ab6640a7ac7ccaca077c9a898edb
contract_base: 45bd2fab992d36eb873b7c448fbd9739f0cad43c
b5_commit: cd443eaf64e1ab6640a7ac7ccaca077c9a898edb
d5_commit: e179358
c5_delivery: C-owned patch and report included by A5
verdict: PASS
ready_for_gate: yes
terminal_state: A5 is complete and G5 is not published
```

任务开始时工作树已有 D5 并行变更；A 未修改、覆盖或提前提交这些文件。D5 随后以独立提交
`e179358` 推送。C5 按计划不自行执行 Git 操作，向 A 交付 C-owned 补丁和
`tests/fixtures/deepseek/youhua_c5/report_to_a.md`；A5 对补丁复核后纳入本批唯一提交。

## 2. A5.1 完整 diff Review

审查基线为 `CONTRACT_BASE=45bd2fab992d36eb873b7c448fbd9739f0cad43c`，终态覆盖
A2-A5、B2-B5、C2-C5、D2-D5 以及 G2-G4 已发布变更。审查不是只看最终文件列表，还逐项
复核公共接缝、活动实现、失败降级和对应回归。

| 审查项 | 结论 | 证据与判断 |
| --- | --- | --- |
| 依赖方向 | PASS | AST 架构与 app factory 契约通过；`domain` 无配置/时钟/I/O，`application` 不导入 `infra`/Flask，Web 只调用只读用例，`bootstrap.py` 仍是唯一组合根。 |
| 额外 HTTP | PASS | application/domain/Web Python 没有新增外部 HTTP；浏览器 `fetch` 只访问本地 `/api`。C5 保持 long 零请求、每次物理尝试先原子预留预算，未增加隐藏重试或 shadow 请求。 |
| 重复扣分 | PASS | `local_score` 只在本地风险扣除后进入固定 `0.68/0.32` 融合，融合只再减本地规则映射后的 DeepSeek 风险；固定向量仍为 `83.40`。 |
| 冻结竞态 | PASS | 正式冻结不可覆盖，普通草稿与恢复统一 P6-first；P6 拒绝不推进 RuntimeState/checkpoint/SSE。D5 又固定订阅打开时 server sequence，消除游标原因的生成器竞态。 |
| SQLite 热读 | PASS | current 与最近 20 日 resident 由 `PublishedSnapshotIndex` 内存读取；只有更老日期 miss 才按日期 single-flight 访问 archive 并预取三策略。Web 路由未直连仓储。 |
| 队列/缓存边界 | PASS | worker 由 semaphore 限制 worker+queue 容量；SSE history、每客户端队列、订阅数和延迟样本均有上限；P1-P6、ReviewCache、P6 current/resident/cold 与单视图字节数均有固定容量。 |
| 临时双跑 | PASS | 完整 Eastmoney/Sina 行情走 columnar 主路径；partial、reference、Tencent overlay 或列式失败才走保守 scalar fallback，没有持续 shadow 双算、双 HTTP 或双份长期驻留。 |
| 调试开关 | PASS | 未新增生产 debug、双跑或测试专用运行开关；既有 server `debug` 仍由严格配置解析，默认运行与验收不依赖调试模式。 |
| 测试豁免 | PASS | 未新增 `noqa`、`type: ignore`、coverage 排除或跳过门禁。Polars mypy override 只隔离第三方包导入，项目公共边界仍被 mypy 检查。A5-F01 还把复杂度债务基线从 C901 38/PLR0912 16 降为 36/15。 |
| 敏感日志 | PASS | 新增日志不包含 token、Authorization、完整外部请求/响应或模型自由文本；失败记录只保留错误类别、来源、事件 ID 或脱敏状态。 |

### 2.1 Review 发现与修复

#### A5-F01：P6 新方法抬高复杂度债务基线

- 症状：阶段 4 为 P6 增加初始化和 cold-date single-flight 后，严格债务基线被调整为
  `C901=38`、`PLR0912=16`。
- 原因：resident 预载、current 恢复、future ownership、cold 写入和按日期淘汰集中在两个方法。
- 修复：拆出 resident 日期预载、策略 current 初始化、cold future、cold 完整日期装载和淘汰
  私有方法；保持完整日期补足 resident 天数、三策略原子冷读、LRU 日期淘汰和异常传播语义。
- 验证：P6、Web API 和 pipeline 83 项回归通过；文件 Ruff 通过；严格债务降为
  `C901=36`、`PLR0912=15`，其他计数不增。

C5 同时在其终审中闭合严格 V4/Pro schema、可信正向映射、跨时区证据去重、raw facts
cache identity、权威 coverage 分类和 emergency 批次泄漏等发现；D5 闭合主动 resync schema
与 cursor-ahead 分类竞态。两份 owner 报告均给出先失败后通过的回归，A5 复验相应测试。

## 3. A5.2 文档、schema、配置、迁移与测试闭合

| 交付面 | 终态 |
| --- | --- |
| 权威设计 | `docs/software-business-design.md` 记录 384 MiB 硬上限、迁移压力峰值、完整峰值场景、纯 columnar 稳态和后续是否收紧上限的独立决策条件。 |
| 策略权威 | 固定融合、风险、V4 facts、证据上限、预算和 cache identity 已在 `docs/recommendation-strategy.md` 与活动实现一致；A5 没有新增策略契约，因此不制造第二套定义。 |
| schema | 公共 `deepseek_v4_review_facts_v1`、`deepseek_challenger_v1`、P3/P4/P5/P6 和 Web schema 版本不变；C5 只让既有解析拒绝未知/错误类型字段，legacy V3 只保留冻结回放兼容。 |
| 配置 | `cache_logical_bytes=260046848`、`process_peak_rss_bytes=402653184` 保持严格分离；旧 `cache_total_bytes` 和未知键继续启动前拒绝。 |
| 迁移 | SQLite schema 与迁移编号未变化；既有 read-only v17 迁移检查覆盖旧库可读、活动库不回写和失败不污染，因此 A5 不新增空迁移。 |
| 测试 | 新增 A5 交付契约；C5/D5 回归、P6 复杂度重构回归、架构、融合、预算、冻结、SSE、性能、桌面和 wheel 均纳入最终门禁。 |
| CHANGELOG | `Unreleased` 按用户诉求、现状/原因、实际行为变化、验证和剩余风险归档 A5 批次。 |

最终内存证据：

- 迁移压力场景：旧/新 scalar/columnar epoch、六池约 70%、8 股 DeepSeek、20 个 P6 日期、
  cold prefetch、原子替换与 32 慢客户端并存；逻辑缓存 `205,468,511` 字节，峰值 RSS
  `387,186,688` 字节，结束 USS `339,656,704` 字节，Polars `1,282,816` 字节。
- 纯 columnar 100 tick：逻辑缓存 `29,661,328` 字节，增长 `0.0%`，峰值 RSS
  `273,195,008` 字节，结束 RSS `254,447,616` 字节，结束 USS `240,578,560` 字节。
- 两组数据都来自 Python 3.14.4 固定离线 fixture。是否收紧 384 MiB 必须另立任务收集更多
  Python/宿主/真实负载证据；本批不把余量变成缓存或业务容量。

## 4. A5.3 B5/C5/D5 签字

| Owner | 报告/提交 | 结论 | 剩余风险 |
| --- | --- | --- | --- |
| B5 | `tests/fixtures/market_data/youhua_b5/report_to_a.md` / `cd443ea` | `PASS / ready_for_gate=yes` | 完整实时 columnar 窄路径只覆盖 Eastmoney/Sina；绝对时延受宿主调度影响；真实来源与 Python 3.10-3.13 未在当前宿主实跑。 |
| C5 | `tests/fixtures/deepseek/youhua_c5/report_to_a.md` / A5 内含 patch | `PASS / ready_for_gate=yes` | DeepSeek 真实供应商时延和可用性仍属外部风险；fixture model 名称产生已知非失败 warning。 |
| D5 | `docs/reports/youhua-d1-p6-web.md` 第 12 节 / `e179358` | `PASS / ready_for_gate=yes` | Firefox SWGL warning 属宿主日志且未形成 DOM/WebDriver/SSE 失败；真实网络与 Python 3.10-3.13 仍待外部矩阵。 |

三方均未申请公共接口变化，未报告已知未解决 owner 缺陷。共同剩余风险是：当前证据不能
证明真实供应商时延、所有支持 Python/宿主组合或推荐收益；失败降级和本地可用性已有自动
回归，但这些外部结论不能由固定 fixture 代替。

## 5. A5.4 不改写已推送历史

`docs/plan_youhua.md` 原建议在阶段 5 squash；仓库级 `AGENTS.md` 明确禁止 amend、squash、
复用此前任务提交或把多个“继续”批次压入同一提交。后者是当前执行的强制仓库规则，因此：

- 保留 G1-G4、A2-A4、B/D owner 已推送提交及其审计映射；
- 不 force-push、不 amend、不重写 `origin/branch`；
- 只为本次 A5 批次创建一个新的 Conventional Commit；
- C5 按交接明确留下的未提交 C-owned 补丁随 A5 一并提交，其报告保留路径和 owner 归属。

内部关键提交对照：

| 里程碑 | 提交 |
| --- | --- |
| CONTRACT_BASE / A1 | `45bd2fa` |
| A2 / G2 | `9477514` / `f482ea1` |
| A3 / G3 | `812de43` / `7a8a028` |
| B4 / D4 / A4 / G4 | `69de151` / `cad5910` / `8e7ab24` / `05594c7` |
| B5 / D5 | `cd443ea` / `e179358` |
| C5 / A5 | C5 patch 由本次唯一 A5 提交承载 |

## 6. A5.5 验证与推送条件

最终验证要求并执行：

- `make format-check`
- `make lint`
- `make type-check`
- `make test`
- `make package`
- 仓库外安装 wheel 后导入 `trader`、执行 `trader-cli --help` 并读取模板、CSS、JavaScript
  和图标资源
- Firefox 152.0.4 在 1280x720、1440x900、1920x1080 精确视口检查 18 行、唯一代码、
  页面溢出、关键重叠、抽屉三分区、浏览器错误和真实 SSE patch
- 提交后分别读取本地 `HEAD` 与 `@{upstream}`，只有哈希一致才完成 A5.5

三档 Firefox 均无白屏、页面横向溢出、关键重叠或浏览器错误；抽屉完整位于视口。连续
两次推荐 patch 的完整推荐 GET 增量为 0，patch 应用增量为 2。Git 哈希一致性属于提交后的
外部状态检查，不能自引用写入同一提交；本报告固定其完成条件，最终交付回执记录实际哈希。

```text
A5 is complete and G5 is not published
```

# 软件设计与运行说明

本文档只描述模块边界、数据流、存储、接口和运维。荐股目标、评分因子、时间线、买入/退出、收益标签、DeepSeek 业务边界及晋级门槛统一见 [strategy_and_prediction.md](strategy_and_prediction.md)，此处不复制第二套业务口径。

## 1. 系统边界

工程是本地 Flask 研究看板，负责：

- 获取并标准化 A 股实时行情和历史日线；
- 运行三个策略评分器并生成推荐快照；
- 保存点时候选、信号和冻结执行策略；
- 回填到期结果，计算成本、组合基线和 OOS 指标；
- 在独立后台任务中预计算 DeepSeek 结构化证据特征；
- 提供推荐、个股诊断、验证和运维接口。

系统不连接券商、不发送订单、不管理真实资金，也不保证收益。

## 2. 分层结构

```text
Flask路由 / 前端
        ↓
services/app_services.py
        ↓
推荐编排 / 快照编排 / 验证编排 / 后台任务
        ↓
三策略评分器      DeepSeek预计算      结果回填与OOS
        ↓                 ↓                   ↓
行情与因子       点时特征仓储        SQLite验证仓储
```

原则：

- HTTP 请求不直接调用 DeepSeek API；
- 评分器不直接操作数据库；
- 仓储不包含策略判断；
- 退出模拟只消费冻结策略和未复权价格；
- 研究产物不能覆盖原始推荐信号。

## 3. 主要模块

### 3.1 HTTP 和应用服务

| 文件 | 职责 |
|---|---|
| `stock_analyzer/app.py` | Flask 初始化、路由注册和依赖容器 |
| `stock_analyzer/runtime.py` | 进程级后台组件所有权与启停顺序 |
| `stock_analyzer/background_workers.py` | 共享停止信号、线程去重、异常回滚和限时回收 |
| `stock_analyzer/services/app_services.py` | 推荐、个股预测、验证和后台刷新用例 |
| `stock_analyzer/recommendation_runtime_support.py` | 三策略编排、验证门控、预计算特征只读接入 |
| `stock_analyzer/app_runtime_support.py` | 推荐响应公共元数据与风险摘要 |
| `stock_analyzer/app_response_support.py` | 响应结构和保存快照兜底 |
| `stock_analyzer/validation_runtime_support.py` | 自动快照与结果回填 worker |

### 3.2 策略和执行

| 文件 | 职责 |
|---|---|
| `stock_analyzer/strategies/today.py` | 今天策略稳定入口 |
| `stock_analyzer/strategies/tomorrow.py` | 明日策略稳定入口 |
| `stock_analyzer/strategies/swing_2_5d.py` | 2–5 日策略稳定入口 |
| `stock_analyzer/scoring_core/` | 候选过滤、因子、评分数学、解释、主题限制和市场状态 |
| `stock_analyzer/execution_policy.py` | 构建随信号冻结的执行和成本策略 |
| `stock_analyzer/risk_rules.py` | 固定退出、止盈止损、移动止损和跌停延迟 |
| `stock_analyzer/snapshot.py` | 点时上下文、评分、特征读取和原子冻结 |

### 3.3 DeepSeek

| 文件 | 职责 |
|---|---|
| `stock_analyzer/deepseek/feature_schema.py` | 四策略 Prompt 版本、五维决策契约、证据边界和严格输出校验 |
| `stock_analyzer/deepseek/payload_builder.py` | 构建不含本地最终排名的结构化请求 |
| `stock_analyzer/deepseek/budget.py` | 188 次每日硬上限、分策略/分窗口原子预算和使用统计 |
| `stock_analyzer/deepseek/production_merge.py` | 75/25 综合评分、veto、周期匹配、today 阶段降级和三组排名 |
| `stock_analyzer/deepseek/feature_dependencies.py` | 仅供结构化特征任务使用的 HTTP、新闻和环境配置装配边界 |
| `stock_analyzer/deepseek/feature_service.py` | 截止时间前的批量 API 预计算、缓存和降级 |
| `stock_analyzer/deepseek/runtime_features.py` | 推荐链数据库只读接入；没有 HTTP 依赖 |
| `stock_analyzer/deepseek/meta_model.py` | 影子 Meta artifact、推断和 G0–G4 门控 |
| `stock_analyzer/deepseek/meta_training.py` | 扩窗 OOS、移动块自助和同池反事实 |
| `stock_analyzer/deepseek/http_client.py` | HTTP 超时、响应和有限重试边界 |
| `stock_analyzer/deepseek/cache.py` | 原子 JSON 缓存 |

旧 rerank、市场复核和通用 Prompt 模块暂作为兼容研究代码存在，但生产入口在 `app_runtime_support.py` 被 `DEEPSEEK_ALLOW_SYNC_RECOMMENDATION_CALLS=False` 硬性阻断，新的特征预计算也不再导入旧 `service.py`。

### 3.4 数据与验证

| 文件 | 职责 |
|---|---|
| `stock_analyzer/providers.py` | 实时/历史数据源和健康状态 |
| `stock_analyzer/factors.py` | 历史量价因子 |
| `stock_analyzer/point_in_time.py` | 候选点时快照和来源时间审计 |
| `stock_analyzer/strategy_validation.py` | `StrategyValidationStore` 门面与结果计算 |
| `stock_analyzer/validation_repository.py` | SQLite 查询、保存和 repository façade |
| `stock_analyzer/validation_schema.py` | 幂等 schema 迁移 |
| `stock_analyzer/validation_outcomes.py` | 结果回填、成本记录和基准关联 |
| `stock_analyzer/portfolio_baseline.py` | 日级 Top K、现金和比较组组合基线 |
| `stock_analyzer/oos_report.py` | 样本外报告和就绪门控 |

## 4. 推荐数据流

```text
实时行情
  → prepare_candidates
  → 历史/事件/基本面点时特征
  → score_today / score_tomorrow / score_swing / long_term_watch
  → 宽候选读取DeepSeek预计算决策
  → final_score = local*75% + deepseek*25% - risk_penalty
  → veto、周期匹配和本地硬过滤复核
  → 后端验证门控
  → 主题与展示收口
  → 内存缓存和推荐快照
```

`attach_persisted_deepseek_features` 只调用 `StrategyValidationStore.latest_deepseek_candidate_features`，不发 HTTP 请求。即使数据库不可用、无特征或读取失败，也标记 `local_only` 并以 `final_score=local_score` 正常返回。

推荐入口使用内存缓存和本地快照降低慢数据源延迟。同一缓存键只允许一个后台刷新线程；后台失败通过响应 `meta` 或 `health` 暴露，不能中断已有可用结果。

## 5. DeepSeek 预计算数据流

```text
stock_analyzer.jobs deepseek-precompute
  → snapshot.build_deepseek_precompute_rows
  → NewsContextProvider补充公告/新闻
  → feature_schema生成点时evidence
  → 证据哈希缓存命中检查
  → 截止时间检查
  → DeepSeek批量请求（最多一次/策略）
  → 严格Schema与证据子集校验
  → deepseek_analysis_batches
  → deepseek_candidate_features
```

约束：

- 每个策略一个批量请求，不逐股调用；候选池由本地先构建，默认最多 120 只；
- 调度按共享预热、today 开盘观察/主执行/降级、午后三策略、14:20 补审分流，today 不能占用午后三策略预算；
- 缓存命中和无证据弃权不计入 API 次数，所有策略和模型共享每日 188 次硬上限，并受分策略和分窗口上限约束；
- API Key 只来自环境变量；
- 调用不跨 14:48 生产截止时间重试，14:50 冻结最终推荐；
- 迟到批次状态为 `late_shadow`；
- 推荐查询不接受迟到批次；
- API 异常只记录批次错误，不生成负向生产分。

## 6. 快照与冻结

`run_snapshots` 只构建一次共享点时上下文，再依次运行三个策略。每个信号批次保存：

- 策略与版本；
- 信号时间、市场数据截止和来源时间；
- 完整候选点时快照；
- 入选行和完整原始 JSON；
- 不可变执行策略及版本哈希；
- 生产基线 provenance；
- DeepSeek 预计算状态和结构化特征。
- 纯本地、DeepSeek、最终综合三组 Top5 代码及逐行 `local_score/deepseek_score/risk_penalty/final_score`。

带截止要求的批次在数据库写事务前再次校验 `freeze_deadline`。超过截止抛出 `SignalFreezeDeadlineExceeded`，避免生成名义上准时、实际上迟到的信号。

### 6.1 15:00 后推荐策略（含 2-5 日自动保存）

我们把规则固定为一套固定时序，避免“有时有、有时没有”的歧义：

- 时间边界
  - 14:50：当日盘中冻结截止，防止盘后覆盖已有的可交易盘中快照。
  - 15:00：开始允许收盘补缺，优先补齐并持久化当日缺失策略。

- 推荐口径分层（两个状态，需清晰展示）
  - 盘中口径：交易时点为信号时刻，标记为“盘中冻结”。
  - 收盘口径：交易时点为当日收盘价，标记为“收盘补充”。
  - 任何返回都要告知本次展示口径，避免用户误解为同一口径。

- 每日同一策略的展示与保存规则
  - 同一交易日内优先使用当天已保存结果，不重复计算。
  - 若某策略还没在盘中保存，且已到收盘后：才补生成并写库。
  - 若盘中已保存并且有效，则本次直接复用盘中口径，不再写入收盘补充。
  - 若有多策略同时请求，按策略分别独立判断，不以“某个策略有值”替代其他策略。

- 15:00 后第一类场景
  - 当天首次有人访问 `swing-picks`、`tomorrow-picks`（以及三策略总览）：
    - 检查当天是否已有盘中保存；
    - 缺失项补一个当日收盘口径；
    - 将新结果立即写入验证库。
  - 目的：第一次看到就有结果，后续调用命中数据库，不重复计算。

- 15:00 后第二类场景（后续重复访问）
  - 同一交易日再次访问时：
    - 直接命中当日已保存记录；
    - 仍保留第一次的口径与时间戳，不再触发重复重算。

- `run.sh` 的两种执行形态
  - 常规启动：仅提供服务，不改写“收盘补缺优先级”。
  - 收盘完整链路（`--after-close`）：先补齐缺失的当日收盘口径，再做回填、基线和因子刷新，确保收盘补充结果进入完整验证链路。

- 与 2-5 日策略关系
  - `swing_picks` 与其他 Horizon 策略共享同一套规则：
    - 到点后先复用；
    - 缺失才补；
    - 有结果即持久化；
    - 口径清晰显示。这样 2-5 日不会出现“页面显示但未入库”或“入库后被覆盖”的问题。

- 稳定性要求（回归底线）
  - 不允许盘后覆盖盘中已冻结快照；
  - 不允许同一日多次重复写入同策略不同批次；
  - 任何口径切换必须在返回元数据里可见；
  - 关键时间来自交易日交易时钟，不能用请求到达的本地时间替代业务时钟。

## 7. 结果回填数据流

```text
到期信号
  → 读取冻结ExecutionPolicy
  → 获取未复权原始日线
  → 按策略定位参考入场
  → simulate_exit
  → 策略outcome
  → 成本情景与execution record
  → 市场/行业/风格基准
  → 指标与组合基线
```

未成熟、停牌、无法买卖或数据缺失使用显式状态，不能静默写为零收益。旧版本的历史集合竞价执行分支只用于重算既有旧信号，不再生成新策略信号。

## 8. SQLite 数据域

默认数据库为 `.runtime/strategy_validation.sqlite3`。主要表：

- `strategy_signal_batches`：策略批次和冻结元数据；
- `strategy_signals`：入选信号、排名、价格和 `raw_json`；
- `strategy_candidate_snapshots`：完整候选、资格、特征和来源时间；
- `strategy_outcomes`：收益、退出和诊断字段；
- `strategy_execution_records`：数量、成本、成交状态和可晋级状态；
- `daily_portfolio_baselines`：日级 Top K 和比较组；
- `strategy_fold_predictions`：扩窗预测审计；
- `strategy_oos_reports`：OOS 报告快照；
- `deepseek_analysis_batches`：API 批次审计；
- `deepseek_candidate_features`：点时结构化特征；
- `deepseek_counterfactual_outcomes`：同池影子增量收益。

Meta 训练通过 `DailyPortfolioBaselineService.candidate_execution_dataset` 读取冻结的完整合格候选池，并用与组合基线相同的执行策略计算未入选候选结果。初始训练窗口与真实 OOS 计数分离；缺失 DeepSeek 特征的候选不会被删除。完整 Meta 还会与只含本地字段的消融 Meta 配对，避免把本地校准效果归因给 DeepSeek。

`ValidationSchemaManager` 使用 `schema_migrations` 幂等执行迁移。新增字段或表必须通过迁移，不允许运行时手改数据库。

## 9. HTTP 接口

### 推荐与健康

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/recommendations` | 三策略推荐 |
| GET | `/api/recommendations/latest` | 最近冻结推荐快照 |
| GET | `/api/recommendations/stream` | SSE 更新 |
| GET | `/api/tomorrow-picks` | 明日策略 |
| GET | `/api/swing-picks` | 2–5 日策略 |
| GET | `/api/health` | 数据源、缓存和任务健康 |

### 个股和验证

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/stock-prediction/<code>` | 本地个股诊断；不再同步请求 DeepSeek |
| GET | `/api/strategy-validation` | 批次、指标和已保存归因 |
| GET | `/api/strategy-validation/daily` | 某日股票明细 |
| POST | `/api/strategy-validation/snapshot` | 手动快照 |
| POST | `/api/strategy-validation/update` | 回填结果 |
| GET | `/api/strategy-validation/readiness` | OOS 和工程就绪状态 |
| GET/POST | `/api/strategy-validation/portfolio-baseline` | 组合基线 |

验证 GET 请求不得触发收费 API。

## 10. 后台任务

统一入口：

```bash
python -m stock_analyzer.jobs <command>
```

| 命令 | 用途 |
|---|---|
| `deepseek-precompute` | 盘中预计算点时证据特征 |
| `snapshot` | 冻结三个策略信号 |
| `update-outcomes` | 回填到期结果 |
| `build-portfolios` | 构建日级组合基线 |
| `deepseek-meta-build` | 生成影子 Meta artifact 和反事实 |
| `validate` | 数据就绪度审计 |
| `backup` | 验证库备份 |
| `stats` | 数据库和迁移健康快照 |

任务使用 SQLite lease 防止相同命令或调度槽位并发运行。直接运行 `app.py` 时，进程级 `RuntimeSupervisor` 从 09:15 预热到 14:20 补审按 `DEEPSEEK_PRECOMPUTE_TIMES` 启动 DeepSeek 变化检查，并统一拥有实时行情、推荐池行情、DeepSeek、自动快照和结果回填线程。DeepSeek 将一次逻辑审查拆为最多 8 只股票的物理小批次，每个 HTTP 请求独立预留预算并落库，逻辑汇总批次只负责覆盖率和状态，不计入 188 次硬上限。周期 worker 共享停止信号；推荐池行情等一次性 worker 由独立服务持有线程引用、拒绝停止期间的新任务并在统一超时内等待。关闭顺序先停止实时调度生产者，再回收其派生 worker；部分启动失败会停止已经启动的同组线程。应用 HTTP 请求线程不执行这些任务。`create_app()` 默认只组装依赖，其他 WSGI 入口必须显式使用 `create_app(start_runtime=True)`，或完全交由 cron、systemd timer、任务平台调度，不能同时启用两套所有者。

## 11. 本地文件

| 路径 | 内容 |
|---|---|
| `.runtime/latest_quotes.json` | 最近行情快照 |
| `.runtime/latest_recommendations.json` | 最近推荐快照 |
| `.runtime/strategy_validation.sqlite3` | 验证数据库 |
| `.runtime/market_data.sqlite3` | 历史日线 |
| `.runtime/deepseek_feature_cache.json` | 证据哈希特征缓存 |
| `.runtime/deepseek_meta_<strategy>.json` | 策略独立影子 artifact |
| `.runtime/weights.json` | 人工确认后的权重覆盖 |
| `.runtime/risk_blacklist.json/.csv` | 风险黑名单 |

运行时文件不纳入源码版本控制，也不能作为源码回滚依据。

## 12. 生产冻结

机器事实源为 `config/runtime.json` 中的 `production_baseline`。启动时 `PRODUCTION_FREEZE_ENABLED` 会锁定：

- 策略版本；
- 候选阈值和 Top K；
- 评分权重；
- 信号截止和执行策略；
- 允许进入生产的开关。

推荐和验证信号携带 generation provenance。配置、代码输入或权重指纹漂移时必须建立新基线，不能把结果并入旧实验。

固定 25% 五维结构化评分已在 `deepseek_25pct_production_2026_07_16` 基线中进入生产；学习型 DeepSeek Meta artifact 仍硬性不进入生产，即使统计门槛通过也需要新基线和人工确认。

## 13. 备份和恢复

数据库备份使用工程现有备份命令。恢复前先备份当前文件，且不能直接覆盖正在被进程使用的 SQLite 数据库。指标异常时先检查策略版本、迁移、批次和 baseline，不要通过删除 `.runtime` 规避问题。

## 14. 维护要求

- 业务口径只修改 `strategy_and_prediction.md`，软件结构只修改本文档。
- 新增慢调用必须有明确后台边界、超时、并发去重和本地降级。
- 新增验证字段必须覆盖 schema、repository、回填、指标、接口和前端。
- 新模型必须保存训练截止、特征版本、Prompt 版本、OOS 结果和 artifact 哈希。
- 任何研究代码不得修改已冻结信号。
- 工作树可能包含用户的其他修改；实施前后都要检查 `git status`，不得回退未知差异。

本轮按用户要求不运行测试；静态检查不替代后续单元、集成、迁移和回归测试。

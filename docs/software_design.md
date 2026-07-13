# 软件设计与运行说明

本文档是系统架构、接口、数据、调度和运维的唯一说明。荐股规则、收益口径和执行门控见 [strategy_and_prediction.md](strategy_and_prediction.md)。

## 0. 文档边界与当前工程状态

原科学化优化计划和生产冻结说明已经合并到本文档和 [strategy_and_prediction.md](strategy_and_prediction.md)：

- 荐股策略、评分取舍、收益指标、DeepSeek 业务边界、OOS 晋级标准进入 `strategy_and_prediction.md`。
- 软件架构、接口、数据库、生产冻结、试验登记、readiness 审计、任务门控和测试维护进入本文档。

截至 2026-07-12 本轮复核：

- 本轮工作树包含未提交工程改动；继续任务前必须运行 `git status --short`，不得回退未知改动。
- 无 sudo 权限环境中已解包本地 sqlite3 CLI：`/tmp/sqlite3-local/usr/bin/sqlite3`，版本 `3.37.2`；系统级安装未执行。
- `build_execution_policy` NameError 已修复并覆盖回归测试。
- `FoldPrediction` 持久化接口、schema 迁移、repository façade 和 `StrategyValidationStore` 便捷方法已补齐。
- OOS 报告已输出 `readiness` 和 `blockers`，自动更新会把 `empty/insufficient_oos_days` 纳入 OOS 告警。
- 当前 `.runtime/strategy_validation.sqlite3` 真实 OOS 仍为 0，P3/P4/P5/P6/P7 均 blocked；不得用回放样本替代真实交易日。

## 1. 系统边界

项目是一个本地 Flask 看板，负责：

- 获取和标准化 A 股实时行情与历史日线。
- 生成盘中观察、明日优先和2-5日持有结果。
- 可选调用 DeepSeek 做候选风险复核。
- 保存信号、回填真实收益、执行验证门控和 OOS 调参。
- 提供个股预测、验证复盘、数据备份和盘后流水线。

系统不负责实盘下单，也不提供收益保证。

## 2. 目录与模块

### 2.1 HTTP 与运行时编排

| 文件 | 职责 |
|---|---|
| [app.py](../stock_analyzer/app.py) | Flask 初始化、路由、缓存、后台刷新和接口响应 |
| [recommendation_runtime_support.py](../stock_analyzer/recommendation_runtime_support.py) | 三策略编排、批量 DeepSeek、市场 gate、验证 gate |
| [app_runtime_support.py](../stock_analyzer/app_runtime_support.py) | DeepSeek 接入、复盘节奏、元数据收口 |
| [app_response_support.py](../stock_analyzer/app_response_support.py) | 统一响应和保存快照兜底 |
| [validation_runtime_support.py](../stock_analyzer/validation_runtime_support.py) | 自动快照、回填和调参 worker |

### 2.2 策略与预测

| 文件 | 职责 |
|---|---|
| [scoring.py](../stock_analyzer/scoring.py) | 候选准备、评分、过滤、分层和风险解释 |
| [strategies/](../stock_analyzer/strategies) | 三策略稳定入口和名称映射 |
| [prediction.py](../stock_analyzer/prediction.py) | 个股本地预测与多策略共识 |
| [stock_optimization.py](../stock_analyzer/stock_optimization.py) | 个股 DeepSeek 优化建议结构 |
| [portfolio.py](../stock_analyzer/portfolio.py) | 组合仓位、主题和市场暴露限制 |

### 2.3 数据与验证

| 文件 | 职责 |
|---|---|
| [providers.py](../stock_analyzer/providers.py) | 实时行情、历史行情和数据源健康状态 |
| [market_data.py](../stock_analyzer/market_data.py) | 本地历史日线下载和存储 |
| [factors.py](../stock_analyzer/factors.py) | 历史量价因子计算 |
| [factor_snapshot.py](../stock_analyzer/factor_snapshot.py) | 因子快照生成 |
| [factor_ic.py](../stock_analyzer/factor_ic.py) | 因子 IC 统计 |
| [strategy_validation.py](../stock_analyzer/strategy_validation.py) | 信号、收益、跳过记录、DeepSeek 归因和 stance 数据库 |
| [validation_replay.py](../stock_analyzer/validation_replay.py) | 当前生产逻辑的历史回放 |
| [calibrate.py](../stock_analyzer/calibrate.py) | walk-forward OOS 权重校准 |

### 2.4 DeepSeek

| 文件 | 职责 |
|---|---|
| [deepseek_client.py](../stock_analyzer/deepseek_client.py) | API 配置、缓存、批量复核、JSON 解析和降级 |
| [deepseek_scheduler.py](../stock_analyzer/deepseek_scheduler.py) | 交易时段槽位、按需调用、每日限额和结果复用 |
| [deepseek_rules.py](../stock_analyzer/deepseek_rules.py) | 已确认规则的结构化应用 |

### 2.5 前端

| 文件 | 职责 |
|---|---|
| [templates/index.html](../templates/index.html) | 推荐池和策略验证页面骨架 |
| [static/app.js](../static/app.js) | 请求、状态、缓存和页面交互 |
| [static/recommendation-renderers.js](../static/recommendation-renderers.js) | 推荐动作与解释渲染 |
| [static/recommendation-tables.js](../static/recommendation-tables.js) | 三策略表格行渲染 |
| [static/validation-ui.js](../static/validation-ui.js) | 验证结论和策略标签 |
| [static/validation-renderers.js](../static/validation-renderers.js) | 验证批次与收益字段渲染 |
| [static/styles.css](../static/styles.css) | 页面布局和状态样式 |

## 3. 页面结构

系统只有两个主面板：

### 3.1 推荐池

- 顶部显示行情源、候选数、历史因子覆盖、硬过滤、大盘和风险黑名单状态。
- 动作汇总只统计 `execution_allowed=true` 的可执行买入动作。
- 三个周期页签是“盘中观察”“明日优先”“2-5日持有”。
- 严格池为空或验证门控失败时仍可显示备选，但动作列明确显示“仓位0、不执行”。

### 3.2 策略验证

- 切换策略和20/60/120日统计窗口。
- 显示当前策略后端 `validation_gate` 的一句话结论。
- 指标优先使用真实交易日聚合口径，并同时显示真实日数和样本条数。
- 查看保存批次、真实/回放类型、主收益、成本、执行跳过和锚点变化。
- 股票预测与 DeepSeek 优化结果共用工具结果区。
- OOS 区同时显示日级等权组合基线，避免用单股均值替代真实组合收益。

## 4. 推荐请求数据流

### 4.1 总入口

```text
GET /api/recommendations
  -> 读取内存缓存或本地推荐快照
  -> 无可用结果时先同步生成纯本地候选
  -> 后台刷新历史因子、DeepSeek 和完整元数据
  -> build_recommendation_horizons()
  -> 应用市场 gate、策略验证 gate 和响应收口
```

推荐接口不应同步等待所有慢数据。缓存或快照存在时立即返回，并在过期、阶段不完整或行情变化后调度后台刷新。

### 4.2 单策略入口

`/api/tomorrow-picks` 和 `/api/swing-picks` 的读取优先级：

1. 当前进程的 horizon 缓存。
2. 最近保存的验证快照。
3. 返回 `async_refresh_pending` 空占位，同时后台刷新。

明日优先和2-5日持有的保存快照兜底都必须重新应用当前验证门控；门控读取异常时强制降为零仓位备选，不能让旧重点推荐绕过新门控。

### 4.3 SSE

`/api/recommendations/stream` 推送推荐快照变化。前端仍保留普通 HTTP 加载和定时刷新，SSE 不是唯一可用路径。

## 5. 缓存与并发

系统使用进程内锁和后台线程避免重复慢请求：

- 推荐总入口和 horizon 接口分别维护缓存、刷新中集合和快照信息。
- 同一缓存键只允许一个后台刷新线程。
- 验证指标按策略和窗口短时缓存；信号保存或收益回填后必须失效。
- 历史因子请求只同步读取本地库，缺失代码最多分批调度后台下载。
- DeepSeek 使用磁盘响应缓存和交易日调度状态，候选签名未变化时复用上次结果。

所有后台失败都应写入 `health`、`meta` 或状态字段，不允许未捕获线程异常中断主请求。

## 6. HTTP 接口

### 6.1 推荐与健康

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 页面入口 |
| GET | `/api/recommendations` | 三策略总结果 |
| GET | `/api/recommendations/latest` | 最近推荐快照 |
| GET | `/api/recommendations/stream` | SSE 推荐更新 |
| GET | `/api/tomorrow-picks` | 明日优先单策略 |
| GET | `/api/swing-picks` | 2-5日持有单策略 |
| GET | `/api/health` | 数据源、因子和 DeepSeek 调度状态 |

常用参数是 `top_n` 和 `market=all|main|chinext|star`。`top_n` 还会受服务端展示上限约束。

### 6.2 个股预测

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/stock-prediction/<code>` | 本地个股预测；`deepseek=1` 才请求优化建议 |
| GET | `/api/stock-prediction/stance-validation` | stance 回填统计 |
| POST | `/api/stock-prediction/stance-validation/update` | 更新 stance 结果 |

### 6.3 策略验证

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/strategy-validation` | 批次列表、指标、后端门控和 DeepSeek 归因 |
| GET | `/api/strategy-validation/daily` | 某日股票明细 |
| POST | `/api/strategy-validation/snapshot` | 手动保存策略快照 |
| POST | `/api/strategy-validation/update` | 回填真实收益 |
| GET | `/api/strategy-validation/auto-update-status` | 自动任务状态 |
| GET | `/api/strategy-validation/readiness` | 全库 OOS、组合、DeepSeek 事件门槛和 P3/P4/P5/P6/P7 blocker |
| POST | `/api/strategy-validation/prefetch-history` | 预取历史行情 |
| POST | `/api/strategy-validation/backfill-samples` | 生成生产逻辑回放样本 |
| GET/POST | `/api/strategy-validation/tuning` | 查看或生成影子调参建议 |
| GET/POST | `/api/strategy-validation/portfolio-baseline` | 读取或重放日级组合基线；POST 执行重放 |

`GET /api/strategy-validation` 不触发 DeepSeek 复盘，只返回已保存的复盘结果。

### 6.4 研究接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/tomorrow-iteration` | 明日策略校准 dry-run |
| POST | `/api/tomorrow-iteration/apply` | 显式应用通过门控的迭代结果 |
| GET | `/api/backtest` | 独立 AlphaLite 研究回测 |

`/api/backtest` 返回 `scope=alphalite_research` 和 `production_strategy_validation=false`，不能替代生产策略验证。

## 7. 验证数据库

默认数据库是 `.runtime/strategy_validation.sqlite3`，由 `StrategyValidationStore` 幂等初始化。主要数据域：

- `strategy_signal_batches`：策略、版本和信号时间批次。
- `strategy_signals`：股票、排名、信号价格和完整 `raw_json`。
- `strategy_outcomes`：次日、固定持有期、动态退出和回撤结果。
- `strategy_execution_skips`：涨停不可买、高开追涨等执行剔除。
- `strategy_candidate_snapshots`：完整候选池、时点特征、缺失掩码、入选与资格原因。
- `daily_portfolio_baselines`：按 `portfolio_baseline_id + 日期` 幂等保存 Top-5、当前规则、随机、指数、现金和挑战模型审计结果。
- `strategy_deepseek_shadow_signals/outcomes`：被 DeepSeek 剔除样本的反事实结果。
- `deepseek_market_gate_reviews`：大盘判断及事后命中率。
- `strategy_tuning_runs`：本地指标、DeepSeek 复盘和调参门控。
- `stock_prediction_snapshots/outcomes`：可选的个股 stance 跟踪。

策略指标必须隔离当前生产版本、当前回放版本和旧版本。真实样本、回放样本、主推样本和备选样本分别统计。

## 8. 本地文件

| 路径 | 内容 |
|---|---|
| `.runtime/latest_quotes.json` | 最近行情快照 |
| `.runtime/latest_recommendations.json` | 最近推荐快照 |
| `.runtime/recommendation_state.json` | 推荐稳定性状态 |
| `.runtime/market_data.sqlite3` | 历史日线 |
| `.runtime/history_cache.sqlite3` | 按需历史缓存 |
| `.runtime/factor_snapshots.sqlite3` | 因子快照 |
| `.runtime/factor_ic.json` | 因子 IC |
| `.runtime/deepseek_cache.json` | DeepSeek 响应缓存 |
| `.runtime/deepseek_schedule.json` | 当日调用槽位、限额和用量 |
| `.runtime/deepseek_attribution.json` | DeepSeek 归因摘要 |
| `.runtime/weights.json` | 人工确认后的权重和策略 alpha 覆盖 |
| `.runtime/risk_blacklist.json/.csv` | 用户维护的风险黑名单 |

JSON 用于单份最新状态、可重建缓存和人工可读配置；SQLite 用于持续增长、需要按日期或股票查询的历史数据。运行时文件不应纳入源码版本控制或作为源码回滚依据；数据库 schema 变更必须使用幂等建表或 `ALTER` 迁移。

## 9. 自动任务

### 9.1 进程内 worker

- 自动收益回填默认开启，从14:30开始按600秒间隔仅补齐历史未成熟结果，不重复覆盖当日信号；发现 `needs_backfill`、待回填或旧口径 outcome 时会按 current baseline 自动回填，验证页只显示自动回填状态，手动 dry-run/execute 仅作为后端诊断接口保留。
- 自动快照默认开启，尾盘隔夜策略在14:50生成单一不可变快照并在14:55前冻结推荐批次；v11/DeepSeek影子分析在冻结后异步运行。系统不实际下单，验证口径按T日收盘集合竞价入场、T+1收盘退出。2-5日策略仍按下一交易日开盘入场，但入场日止盈止损只记风险事件，最早T+2可卖。
- 可执行生产策略集合 `ACTIVE_STRATEGIES` 只有 `tomorrow_picks` 和 `swing_picks`；自动快照集合 `AUTO_SNAPSHOT_STRATEGIES` 还包含 `short_term`，盘中观察保存为辅助验证样本但不形成买入指令。
- 14:55及以后才生成或尚未完成冻结的尾盘隔夜信号拒绝保存为可执行批次；执行收益只使用未复权原始价，前复权价仅用于因子计算。
- DeepSeek 验证复盘按新增真实交易日节流，默认每新增5日才允许再次调用。

### 9.2 盘后流水线

推荐使用：

```bash
./run.sh after-close --strategy all
```

完整流水线按顺序执行：

1. 下载或更新历史日线。
2. 保存盘中观察、明日优先和2-5日持有快照。
3. 回填已成熟收益和执行跳过。
4. 按冻结规则重放日级等权 Top-5，并更新随机/指数/现金比较组。
5. 刷新因子快照。
6. 刷新因子 IC。
7. 备份验证数据库。

日级组合只纳入完整候选池已结算且排名字段覆盖完整的日期。未成交保留为现金并单独报告，`pending/unknown` 不会被当成零收益；随机组固定种子且至少 1,000 次，汇总输出净收益、累计收益、回撤、Sortino、换手、行业集中、容量利用率和未成交率。

普通办公 PC 可用 `--market-data-limit` 分批下载，避免一次处理全市场。

## 10. DeepSeek 运行配置

DeepSeek 配置同时受 [config.py](../stock_analyzer/config.py) 和环境变量控制。关键默认值：

```text
ENABLE_DEEPSEEK_RUNTIME=1
DEEPSEEK_SHADOW_ONLY=1
DEEPSEEK_ENABLED=<存在 API key 时自动开启>
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_PRO_MODEL=deepseek-v4-pro
DEEPSEEK_BLEND_ALPHA=0.15
DEEPSEEK_BATCH_REVIEW_LIMIT=15
DEEPSEEK_CACHE_ENABLED=1
DEEPSEEK_CACHE_TTL_SECONDS=86400
DEEPSEEK_SCHEDULE_ENABLED=1
DEEPSEEK_SCHEDULE_STRATEGIES=tomorrow_picks
DEEPSEEK_DAILY_CALL_CAP=30
DEEPSEEK_DAILY_PRO_CALL_CAP=5
DEEPSEEK_LATE_MIN_INTERVAL_SECONDS=60
ENABLE_DEEPSEEK_NEWS_CONTEXT=0
ENABLE_DEEPSEEK_MARKET_GATE=0
```

调度规则：早盘和午后14:30前按半小时槽位，14:30-15:00按需调用并设置最短间隔。每日计数、模型层级、token 用量和最近结果保存在调度状态文件中。

## 11. 生产冻结、试验登记和 readiness 审计

### 11.1 生产冻结

P0 的机器事实源是 `config/production_baseline.json`。启动时默认启用 `PRODUCTION_FREEZE_ENABLED=1`，策略版本、明日策略 Top-K、候选过滤阈值、退出规则和生产权重均按该清单锁定。七个实验性开关被强制关闭；DeepSeek 可以继续调用和记录，但只形成 shadow 对照，不改变生产排序、过滤或仓位。

每个推荐响应和验证信号都携带 `generation`，其中包含：

- 基线 ID、策略版本和排序字段。
- 完整有效开关。
- 权重、输入、输出指纹。
- 重放上下文。

`baseline_status=drift_detected` 表示代码或运行配置与冻结清单不一致，不能把结果混入同一实验基线。

### 11.2 试验登记

试验登记保存在 `experiments/registry.jsonl`，每行一个 JSON 对象。登记必须包含假设、唯一变更、训练窗口、测试窗口、主指标、风险约束、试验族、结果和决定。

```bash
python -m stock_analyzer.experiment_registry list
python -m stock_analyzer.experiment_registry register --record experiment.json
```

`tomorrow_picks` 是首个研究策略，生产 K 固定为 5。所有研究报告同时给出 K=3/5/10，其中 K=3/10 仅作敏感性诊断，不得因表现更好而替换生产 K。

### 11.3 Readiness 审计

不依赖系统级 sqlite3 的本地审计入口：

```bash
PYTHONPATH=. .venv/bin/python -m stock_analyzer.validation_audit_cli \
  --readiness \
  --db-path .runtime/strategy_validation.sqlite3
```

该命令输出关键表计数、真实 OOS 日数、组合基线日数、DeepSeek 事件日数和 P3/P4/P5/P6/P7 blocker。存在 blocker 时返回码为 `1`，用于自动化识别“未就绪”。

同一口径也由 HTTP 暴露：

```text
GET /api/strategy-validation/readiness
```

HTTP 请求成功时 `ok=true`；门槛是否就绪看 `ready`。当前 0 样本状态返回 `ready=false`，并在 `blockers` 中列出 P3/P4/P5/P6/P7 缺口。

当前 0 样本状态应显示：

```text
P3-REAL-OOS-SAMPLE-GATE: observed_days=0, required_days=60
P4-REBUILDABLE-RETURN-ARTIFACT: observed_days=0, required_days=60
P5-PORTFOLIO-ABLATION-EVIDENCE: observed_days=0, required_days=60
P6-DEEPSEEK-EVENT-COUNTERFACTUAL: observed_days=0, required_days=60
P7-GRAY-ROLLBACK: observed_days=0, required_days=120
```

### 11.4 数据契约

以下 JSON 可序列化契约是验证与研究边界。字段只能新增可选项，不能随手改名或改变语义：

| 契约 | 生产者 | 消费者 | 最小字段 |
|---|---|---|---|
| `CandidateSnapshotBatch` | 快照/数据层 | 执行、研究、展示 | 策略/版本、信号日期时间、行情截止时间、完整候选、资格原因、是否入选、原始特征、缺失掩码、来源时间戳、point-in-time 状态 |
| `ExecutionPolicy` / `ExecutionRecord` | 执行层 | 持久化、研究、展示 | policy version、入退场状态、未成交原因、目标/实成交数量和价格、费用/滑点/冲击、毛/净收益、成本情景、原始价格、是否可用于晋级 |
| `DailyPortfolioEvaluation` | 组合基线层 | 研究、展示 | 日期、策略、排序源、Top-K、权重、现金、毛/净收益、换手、集中度、容量、未成交率、市场/行业基准 |
| `FoldPrediction` | 研究层 | 持久化、审计 | experiment/fold ID、训练截止日、测试日期、代码、基线分、预测净收益/概率、是否入选、真实净收益、模型版本 |
| `ModelArtifact` | 研究层 | 生产门控 | baseline ID、feature schema hash、训练截止日、失效时间、特征清单、超参数、逐折摘要、OOS/FDR/CI gate、artifact hash |

## 12. 启动、备份与恢复

启动服务：

```bash
./run.sh
```

默认地址是 `http://127.0.0.1:5000`。`run.sh` 使用项目 `.venv`，并负责代理探测和依赖环境检查。

验证库备份：

```bash
.venv/bin/python -m stock_analyzer.daily_job --backup-validation
.venv/bin/python -m stock_analyzer.daily_job --list-validation-backups
```

恢复前会自动备份当前数据库：

```bash
.venv/bin/python -m stock_analyzer.daily_job --restore-validation <backup-file>
```

不要直接覆盖正在使用的 SQLite 文件，也不要删除 `.runtime` 来解决指标不一致；应先确认策略版本、批次和迁移状态。

## 13. 实施、测试与维护

完整测试：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
```

前端 JavaScript 语法检查：

```bash
node --check static/app.js
node --check static/recommendation-renderers.js
node --check static/validation-ui.js
node --check static/validation-renderers.js
```

维护要求：

- 路由、配置默认值、策略名称或数据流变化时更新本文档。
- 业务收益和执行规则只写入 `strategy_and_prediction.md`，不要在本文档复制第二套解释。
- 新增慢调用必须有缓存、超时、并发去重和本地降级。
- 新增验证字段必须同时覆盖保存、回填、指标、接口、前端和测试。
- 不允许验证页 GET 隐式触发收费调用。
- 不允许旧缓存文本绕过 `execution_allowed`、验证门控或组合过滤。

### 13.1 串行任务门控

当前执行模式是单 Codex 串行推进。每次只做一个明确任务卡，先跑定向测试，再进入下一项。不能压缩以下自然时间门槛：

- `tomorrow_picks` 首次灰度至少 60 个真实 OOS 交易日。
- 正式替换至少 120 个真实 OOS 交易日。
- 灰度上线至少 20 个灰度交易日。
- DeepSeek/事件试验至少 60 个有效事件日。

工程完成不等于收益模型可上线：

- **Engineering complete**：代码、迁移、测试、重放和监控完成，但生产仍使用冻结基线。
- **Shadow eligible**：G3/G4 通过，模型或事件只产生影子预测，不改变排序和仓位。
- **Production eligible**：G5 通过，才允许修改唯一生产开关；否则删除无贡献机制或继续冻结。

### 13.2 当前任务账本

| Task | Wave | Status | Gate | Blocker |
|---|---|---|---|---|
| W1-FOLD-PREDICTIONS | W1 | done | G1 partial | 真实 OOS 样本仍为 0 |
| P1-BUILD-EXECUTION-POLICY-IMPORT | W1 | done | G1 partial | 无；已修复 `name 'build_execution_policy' is not defined` |
| P2-EXPECTED-RETURN-DEHEURISTIC | P2 | done | G2/P4 partial | 仍需真实 OOS 日和单一可重建模型 artifact |
| P2-SCORE-CALIBRATION-DIAGNOSTIC | P2 | done | G3 partial | 仍需由收益模型 OOS 校准替代独立分桶概率 |
| P3-META-EVENT-ENSEMBLE-SHADOW | P3 | done | G3 partial | DeepSeek 仍需 point-in-time 事件反事实试验 |
| P3-DEEPSEEK-RERANK-SHADOW | P3 | done | G3 partial | 仍需累计事件日后做基础模型 vs 基础模型+事件反事实收益试验 |
| P3-FRONTEND-RANKING-LANGUAGE | P3 | done | G3 partial | 前端仍需接入真实 OOS 样本数和 artifact 状态的完整展示 |
| P3-OOS-BLOCKER-READINESS | W3 | done | G3 partial | 已将 `empty/insufficient_oos_days` 暴露为 blocker；真实 OOS 交易日仍为 0 |
| P3-READINESS-AUDIT-CLI | W3 | done | G3 partial | 已提供 `python -m stock_analyzer.validation_audit_cli --readiness`；真实 OOS 交易日仍为 0 |
| P3-REAL-OOS-SAMPLE-GATE | W3 | blocked | G3 blocked | 真实 OOS 交易日为 0，距离 60 日门槛仍缺 60 日 |
| P4-REBUILDABLE-RETURN-ARTIFACT | W4 | blocked | G4 blocked | 真实 OOS 交易日为 0，无法训练、保存或验证可晋级 artifact |
| P5-PORTFOLIO-ABLATION-EVIDENCE | W4 | blocked | G4 blocked | 无真实日级组合收益，无法做逐项消融结论 |
| P6-DEEPSEEK-EVENT-COUNTERFACTUAL | W4 | blocked | G4 blocked | 事件 shadow 信号/结果均为 0，距离 60 个有效事件日仍缺 60 日 |
| P7-GRAY-ROLLBACK | W5 | blocked | G5 blocked | 未通过 G3/G4，且无 20 个灰度交易日 |

### 13.3 分层测试

```bash
./scripts/test.sh fast              # not slow and not integration（默认快反馈）
./scripts/test.sh fast-parallel     # 快反馈并行（已装 pytest-xdist 时）
./scripts/test.sh integration       # integration and not slow
./scripts/test.sh slow              # slow and not integration
./scripts/test.sh slow-integration  # slow and integration
./scripts/test.sh all               # not slow 全量
```

运行建议（落地）:

- 本地开发：先跑 `-m "not (slow or integration)"`，目标是 2~3 分钟内有结果。
- 集成前：再补 `integration and not slow`。
- 每日 CI（短任务）：快速组 + integration。
- 全量 CI（每周 / 定时）：补 `slow` + `slow and integration`。

CI 映射（仓库默认配置）：

- PR 与 Push：`.github/workflows/test-ci.yml` 触发 `fast`。
- 手动调度 / 日常定时：`.github/workflows/test-ci.yml` 依次跑 `integration`、`slow`、`slow-integration`。

建议把 `integration` 与 `slow` 测试保留为：

- 关键模块变更后手动触发 `workflow_dispatch`
- 每周定时任务兜底

子系统建议：

- 节点 JS 合约测试（`tests/test_frontend_contracts.py`）保留在 integration 里，必要时单独调度（`node` 缺失时自动 skip）。
- 避免每次都跑重型快照/回填类用例，除非改动了相关模块。

一键入口（推荐）：

```bash
./scripts/test.sh fast             # 快速路径：not slow and not integration
./scripts/test.sh fast-parallel    # 并行快速路径（需要 pytest-xdist）
./scripts/test.sh integration      # 集成测试
./scripts/test.sh slow             # slow-only（不含 integration）
./scripts/test.sh slow-integration  # slow + integration（最重）
./scripts/test.sh all              # not slow 全量
```

如果你更习惯 Makefile：

```bash
make test-fast
make test-fast-parallel
make test-integration
make test-slow
make test-slow-integration
make test-all
```

CI 运行配置（默认）：

- PR 与 Push：`fast`
- `workflow_dispatch` / 日常定时：`integration`、`slow`、`slow-integration`
- `workflow_dispatch` 与日常定时下，`integration` / `slow` / `slow-integration` 会并行（矩阵并发）执行，并在末尾合并 `test-timings-full-*.csv` 汇总。
- xdist 并发可通过环境变量 `PYTEST_XDIST_WORKERS` 控制（CI 可通过 `ci_xdist_workers` / 仓库变量 `PYTEST_XDIST_WORKERS` 传入；`auto` 表示自动并发，`0` 表示禁用）。
- 阈值默认告警：
  - fast：1200 秒
  - integration：600 秒
  - slow：2400 秒
  - slow-integration：2400 秒
  - 可在 GitHub Actions Repository Variables 覆盖告警阈值：
    - `FAST_MAX_SECONDS`
    - `INTEGRATION_MAX_SECONDS`
    - `SLOW_MAX_SECONDS`
    - `SLOW_INTEGRATION_MAX_SECONDS`
- 可设置：
  - `CI_FAIL_ON_TIMEOUT` / `CI_USE_XDIST`：`true`/`false`，默认 `false` / `true`。
    可通过仓库变量或 `workflow_dispatch` 手动输入覆盖：
    - `CI_FAIL_ON_TIMEOUT`、`CI_USE_XDIST`（手动：`ci_fail_on_timeout`、`ci_use_xdist`）
  - fast 阶段会优先尝试并行执行（`fast-parallel`），若 `CI_USE_XDIST=false` 则退回串行。
- `scripts/test.sh` 与 `Makefile` 行为：
  - `PYTEST_XDIST_WORKERS` 默认为 `auto`（自动并发）；
    设置为 `0` 可关闭并发，适用于有共享状态或偶发污染的环境。
  - `fast-parallel` 若未安装 `pytest-xdist` 将自动降级为串行（告警）。
  - 需要严格要求可用并行时，使用：
    - `PYTEST_XDIST_REQUIRED=1 ./scripts/test.sh fast-parallel`
    - `make test-fast-parallel-strict`

每个任务门控至少执行：

```bash
git diff --check
PYTHONPATH=. .venv/bin/python -m compileall -q stock_analyzer
PYTHONPATH=. .venv/bin/pytest -q
```

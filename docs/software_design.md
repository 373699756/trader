# 软件设计与运行说明

本文档是系统架构、接口、数据、调度和运维的唯一说明。荐股规则、收益口径和执行门控见 [strategy_and_prediction.md](strategy_and_prediction.md)。

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

明日优先保存快照兜底也必须重新应用当前验证门控，不能让旧重点推荐绕过新门控。

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
| POST | `/api/strategy-validation/prefetch-history` | 预取历史行情 |
| POST | `/api/strategy-validation/backfill-samples` | 生成生产逻辑回放样本 |
| GET/POST | `/api/strategy-validation/tuning` | 查看或生成影子调参建议 |

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

运行时文件不应作为源码回滚依据；数据库 schema 变更必须使用幂等建表或 `ALTER` 迁移。

## 9. 自动任务

### 9.1 进程内 worker

- 自动收益回填默认开启，从14:30开始按600秒间隔运行。
- 自动快照默认开启，15:00后使用收盘锚点。
- 默认生产策略集合 `ACTIVE_STRATEGIES` 只有 `tomorrow_picks` 和 `swing_picks`；盘中观察不自动保存为可执行样本。
- 收盘锚点不完整时拒绝把该批次保存为正式回溯锚点。
- DeepSeek 验证复盘按新增真实交易日节流，默认每新增5日才允许再次调用。

### 9.2 盘后流水线

推荐使用：

```bash
./run.sh after-close --strategy all
```

完整流水线按顺序执行：

1. 下载或更新历史日线。
2. 保存明日优先和2-5日持有快照。
3. 回填已成熟收益和执行跳过。
4. 刷新因子快照。
5. 刷新因子 IC。
6. 备份验证数据库。

普通办公 PC 可用 `--market-data-limit` 分批下载，避免一次处理全市场。

## 10. DeepSeek 运行配置

DeepSeek 配置同时受 [config.py](../stock_analyzer/config.py) 和环境变量控制。关键默认值：

```text
ENABLE_DEEPSEEK_RUNTIME=1
DEEPSEEK_ENABLED=<存在 API key 时自动开启>
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_PRO_MODEL=deepseek-v4-pro
DEEPSEEK_BLEND_ALPHA=0.15
DEEPSEEK_BATCH_REVIEW_LIMIT=15
DEEPSEEK_CACHE_ENABLED=1
DEEPSEEK_CACHE_TTL_SECONDS=86400
DEEPSEEK_SCHEDULE_ENABLED=1
DEEPSEEK_SCHEDULE_STRATEGIES=tomorrow_picks
DEEPSEEK_DAILY_CALL_CAP=11
DEEPSEEK_DAILY_PRO_CALL_CAP=1
ENABLE_DEEPSEEK_NEWS_CONTEXT=0
ENABLE_DEEPSEEK_MARKET_GATE=0
```

调度规则：早盘和午后14:30前按半小时槽位，14:30-15:00按需调用并设置最短间隔。每日计数、模型层级、token 用量和最近结果保存在调度状态文件中。

## 11. 启动、备份与恢复

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

## 12. 测试与维护

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

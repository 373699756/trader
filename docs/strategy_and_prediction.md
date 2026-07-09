# 荐股策略与股票预测

本文档只讲两类内容：

- 三类荐股策略：今天、明天、2-5天
- 个股预测与 DeepSeek 优化建议

不再按“某天改了什么”拆分，只保留当前实现。

所有结果只用于研究，不构成投资建议，也不保证盈利。

## 目录

- [阅读导航](#阅读导航)
- [关键文件索引](#关键文件索引)
- [1. 当前启用策略](#1-当前启用策略)
- [2. 三类荐股策略](#2-三类荐股策略)
- [3. 三类荐股如何结合 DeepSeek](#3-三类荐股如何结合-deepseek)
- [4. 策略验证阶段的 DeepSeek](#4-策略验证阶段的-deepseek)
- [5. 个股预测](#5-个股预测)
- [6. 自动保存与回溯](#6-自动保存与回溯)
- [7. 判断策略是否靠谱](#7-判断策略是否靠谱)
- [8. 后续优化路线](#8-后续优化路线)
- [9. 常见坑](#9-常见坑)

## 阅读导航

`当前实现`

- 1. 当前启用策略
- 2. 三类荐股策略
- 3. 三类荐股如何结合 DeepSeek
- 4. 策略验证阶段的 DeepSeek
- 5. 个股预测
- 6. 自动保存与回溯
- 7. 判断策略是否靠谱

`后续规划`

- 8. 后续优化路线

## 关键文件索引

策略与推荐主链：

- [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:643)
- [stock_analyzer/recommendation_runtime_support.py](/home/c/linux/trader/stock_analyzer/recommendation_runtime_support.py:17)
- [stock_analyzer/app_runtime_support.py](/home/c/linux/trader/stock_analyzer/app_runtime_support.py:29)
- [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:735)

本地策略与评分：

- [stock_analyzer/scoring.py](/home/c/linux/trader/stock_analyzer/scoring.py:1)
- [stock_analyzer/strategies/__init__.py](/home/c/linux/trader/stock_analyzer/strategies/__init__.py:1)

个股预测与优化：

- [stock_analyzer/prediction.py](/home/c/linux/trader/stock_analyzer/prediction.py:18)
- [stock_analyzer/stock_optimization.py](/home/c/linux/trader/stock_analyzer/stock_optimization.py:1)

## 1. 当前启用策略

| 中文名 | 策略名 | 推荐上限 | 主验证周期 | 定位 |
|---|---|---:|---:|---|
| 今天推荐 | `short_term` | 18 | 次日 | 盘中强势、量价、人气和风险过滤 |
| 明天推荐 | `tomorrow_picks` | 18 | 次日 | 收盘后筛次日延续和买入安全 |
| 2-5 天推荐 | `swing_picks` | 18 | 2-5 日 | 短周期趋势延续、温和放量、不过热 |

推荐数量是上限，不是必须凑满。门槛下没有合格标的时，允许空推荐。

## 2. 三类荐股策略

### 2.1 共同过滤

三类策略先经过同一批基础过滤：

- 只看沪深主板、创业板、科创板常见 A 股代码。
- 排除 ST、退市、停牌、无有效现价。
- 排除成交额低于 `MIN_TURNOVER` 的低流动性股票。
- 排除极端下跌、涨停附近不可买、明显一字板或过热不可交易样本。
- 默认按行业/主题做集中度限制，避免推荐结果全压在单一行业。

历史因子默认关闭；开启 `ENABLE_HISTORY_FACTORS=1` 后，会额外使用 3/5/10/20 日动量、均线偏离、成交额放大、20 日突破、波动率等指标。

### 2.2 今天推荐 `short_term`

目标：找当天盘中已经走强、但还没有明显透支的短线候选。

主要因素：

- 当日涨幅、涨速、量比、成交额。
- 短周期动量和成交额变化。
- 行业强度、热度、舆情。
- 追高、过热、高换手、负面舆情风险。

适用场景：

- 市场偏强或有清晰主线时更有效。
- 偏防守市只作为观察池，不应因为分高直接追。

验证口径：

- 保存当日候选。
- 次日回填行情。
- 重点看次日上涨个数、下跌个数、方向胜率和平均表现。

### 2.3 明天推荐 `tomorrow_picks`

目标：在收盘前后筛出次日更可能延续、同时仍有买入安全的股票。

主要因素：

- 成交额、换手率、温和涨幅、涨速、量比。
- 收盘结构、日内承接、买入安全。
- 60 日和年内涨幅是否透支。
- 高振幅、高换手、高量比、尾盘假拉升风险。

适用场景：

- 15:00 后结合收盘锚点更可靠。
- 如果只有少数股票满足条件，少推荐或空推荐优于硬凑数量。

验证口径：

- 保存当天的明天推荐批次。
- 以保存锚点价和次日行情核验方向。
- 页面展示有效样本、次日净胜率、涨跌个数和平均表现。

### 2.4 2-5 天推荐 `swing_picks`

目标：找短周期趋势还在、放量温和、没有明显过热的候选。

主要因素：

- 3/5/10 日动量、短均线趋势。
- 温和放量、成交额和流动性。
- 不过热分、波动和振幅控制。
- 行业扩散和市场状态。

适用场景：

- 适合趋势延续和主线扩散行情。
- 对单日涨速不如今天/明天策略敏感，更重视持续性。

验证口径：

- 保存当天 2-5 天候选。
- 后续回填短周期表现。
- 重点看持有期方向胜率、平均收益和风险暴露。

## 3. 三类荐股如何结合 DeepSeek

DeepSeek 在三类荐股里的角色，不是替代本地策略，而是做两件事：

1. 候选排序复核
2. 策略验证复盘与影子调参建议

整体流程：

```text
本地策略先生成候选
  -> DeepSeek 对候选做风险复核和排序调整
  -> 输出今天 / 明天 / 2-5天推荐
  -> 保存快照并回填真实结果
  -> 策略验证阶段再由 DeepSeek 做复盘和影子调参
```

### 3.1 代码入口

主入口：

- [stock_analyzer/recommendation_runtime_support.py](/home/c/linux/trader/stock_analyzer/recommendation_runtime_support.py:17)
- [stock_analyzer/app_runtime_support.py](/home/c/linux/trader/stock_analyzer/app_runtime_support.py:29)
- [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:735)

三类推荐总入口调用链：

```text
/api/recommendations
  -> _recommendations_payload()
  -> _build_recommendations_payload()
  -> build_recommendation_horizons()
  -> apply_deepseek_rerank()
  -> rerank_candidates()
```

明天 / 2-5 天单独入口调用链：

```text
/api/tomorrow-picks 或 /api/swing-picks
  -> _horizon_payload()
  -> _build_horizon_payload()
  -> scored_strategy_rows()
  -> apply_deepseek_rerank()
  -> rerank_candidates()
```

### 3.2 各策略如何接入

今天推荐 `short_term`：

- `score_today_picks(...)` 先产本地候选
- `apply_deepseek_rerank("short_term", ...)`
- DeepSeek 重点看追高、盘中过热、冲高回落风险

明天推荐 `tomorrow_picks`：

- `score_tomorrow_picks(...)` 先产本地候选
- `apply_deepseek_rerank("tomorrow_picks", ...)`
- `apply_tomorrow_validation_gate(...)` 再结合验证样本门控
- DeepSeek 重点看尾盘假拉升、涨停附近不可买、次日兑现风险

2-5 天推荐 `swing_picks`：

- `score_swing_2_5d_picks(...)` 先产本地候选
- `apply_deepseek_rerank("swing_picks", ...)`
- DeepSeek 重点看假突破、高位横盘、2-5 天过热透支

### 3.3 候选复核逻辑

统一入口：

```python
apply_deepseek_rerank(strategy_name, rows, market_filter)
```

职责：

1. 检查 `ENABLE_DEEPSEEK_RUNTIME`
2. 检查 `DEEPSEEK_RERANK_DISABLED_STRATEGIES`
3. 调用 `rerank_candidates(...)`
4. 失败时回退本地排序

### 3.4 DeepSeek 读取哪些候选字段

候选结构化输入来自 [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:274) 的 `_request_payload(...)`。

主要字段包括：

- `code`
- `name`
- `score`
- `pct_chg`
- `speed`
- `volume_ratio`
- `turnover_rate`
- `turnover`
- `amplitude`
- `sixty_day_pct`
- `ytd_pct`
- `ret_5d`
- `ret_10d`
- `ret_20d`
- `ma20_gap`
- `volatility_20d`
- `liquidity_score`
- `momentum_score`
- `trend_score`
- `historical_edge_score`
- `execution_score`
- `tail_setup_score`
- `risk_penalty`
- `failure_reasons`
- `industry`
- `theme`
- `reasons`

### 3.5 融合与剔除

DeepSeek 结果和本地结果的融合在 [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:557) 的 `_merge_ranking_rows(...)`。

会生成：

- `deepseek_rank_score`
- `deepseek_action`
- `deepseek_penalty`
- `deepseek_reason`
- `deepseek_risk_flags`
- `deepseek_profit_flags`
- `deepseek_veto`

高风险候选还会通过 `_deepseek_gate_decision(...)` 被直接过滤，而不只是排后面。

### 3.6 降级逻辑

DeepSeek 不可用时，系统仍然可以产出三类推荐。

降级路径包括：

- runtime 未开启
- API key 缺失
- 某策略被禁用 rerank
- DeepSeek 超时
- DeepSeek 返回无法解析
- DeepSeek 调用异常

此时行为是：

- 保留本地候选和本地排序
- 在 `meta.deepseek` 中记录状态
- 不中断推荐主流程

## 4. 策略验证阶段的 DeepSeek

荐股阶段之外，DeepSeek 还有第二条链路：验证复盘。

入口在 [stock_analyzer/app_runtime_support.py](/home/c/linux/trader/stock_analyzer/app_runtime_support.py:102)：

```python
deepseek_validation_review(validation_store, strategy_name, metrics, days)
```

它会：

1. 读取验证样本 `live_weight_samples(...)`
2. 附加因子快照 `attach_factor_snapshots(...)`
3. 调用 `review_strategy_validation(...)`

真正的 DeepSeek 复盘函数在 [stock_analyzer/deepseek_client.py](/home/c/linux/trader/stock_analyzer/deepseek_client.py:953)。

输出：

- `decision`
- `avoid_conditions`
- `suggested_filters`
- `suggested_penalties`
- `summary`
- `rule_candidates`

这些建议只作为影子调参使用，不自动改正式策略。

当前影子调参方向：

| 策略 | 表现偏弱时优先建议 |
|---|---|
| 今天推荐 | 增加反转修正，降低过热样本权重 |
| 明天推荐 | 提高最低分，放大追高/回落风险惩罚，限制行业集中 |
| 2-5 天推荐 | 提高不过热权重，降低单纯动量追高权重 |

## 5. 个股预测

个股预测主链路对应接口：

- [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:965) `/api/stock-prediction/<code>`

目标不是让 DeepSeek 直接替代本地预测，而是：

- 先用本地量化规则给出个股涨跌倾向和策略命中证据
- 再把这份结构化预测结果交给 DeepSeek 做二次复核
- DeepSeek 重点回答“现在怎么做”，而不是重新算一遍涨跌

因此最终输出分成两层：

- 本地预测：方向、置信度、风险、命中策略、未命中原因
- DeepSeek 优化：小仓试单 / 只观察 / 等确认 / 不追价，以及对应的入场、风控、规避条件

### 5.1 模块边界

- 路由层：
  - [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:965)
- 预测编排层：
  - [stock_analyzer/recommendation_runtime_support.py](/home/c/linux/trader/stock_analyzer/recommendation_runtime_support.py:49)
- 本地预测聚合层：
  - [stock_analyzer/prediction.py](/home/c/linux/trader/stock_analyzer/prediction.py:18)
- runtime 支撑层：
  - [stock_analyzer/app_runtime_support.py](/home/c/linux/trader/stock_analyzer/app_runtime_support.py:142)
- 个股优化层：
  - [stock_analyzer/stock_optimization.py](/home/c/linux/trader/stock_analyzer/stock_optimization.py:1)

### 5.2 调用链

```text
前端输入股票代码
  -> GET /api/stock-prediction/<code>
  -> app.py: stock_prediction()
  -> recommendation_runtime_support.py: prediction_strategy_rows()
  -> prediction.py: build_stock_prediction()
  -> app_runtime_support.py: deepseek_stock_prediction_review()
  -> stock_optimization.py: review_stock_prediction()
  -> 返回 prediction + optimization
```

### 5.3 本地预测如何形成

`stock_prediction()` 的核心步骤：

1. 读取实时行情 `quotes`
2. 生成候选池 `candidates` 和市场环境 `market_regime`
3. 拉舆情分、热门度、行业强度等辅助信息
4. 调 `prediction_strategy_rows(...)` 生成三类策略结果
5. 调 `build_stock_prediction(...)` 输出本地预测结果

本地预测会输出这些关键字段：

- `prediction`
- `horizons`
- `strategy_hits`
- `missed_strategies`
- `market_regime`
- `price`
- `pct_chg`
- `turnover`
- `volume_ratio`
- `sixty_day_pct`

如果候选池里没有这只股票：

- 先尝试实时行情兜底
- 再尝试历史行情兜底
- 最后返回“被过滤/无法判断”的本地结果

### 5.4 个股预测如何接入 DeepSeek

路由挂接点在 [stock_analyzer/app.py](/home/c/linux/trader/stock_analyzer/app.py:1035)：

```python
deepseek_requested = request.args.get("deepseek", "1").lower() not in ("0", "false", "no", "off")
if deepseek_requested and bool(result.get("ok")):
    result["optimization"] = deepseek_stock_prediction_review(result)
```

`deepseek_stock_prediction_review(...)` 会：

1. 检查 `ENABLE_DEEPSEEK_RUNTIME`
2. 根据 `strategy_hits` 推断当前个股更接近哪类策略
3. 捕获异常并返回统一降级结果

主策略推断逻辑：

- 优先取第一条 `strategy_hit.strategy_name`
- 再用 `storage_strategy_name(...)` 归一化
- 如果没有命中任何策略，默认按 `short_term` 处理

### 5.5 DeepSeek 输入 payload

`review_stock_prediction(...)` 会先调 `_stock_prediction_review_payload(...)`，位置在 [stock_analyzer/stock_optimization.py](/home/c/linux/trader/stock_analyzer/stock_optimization.py:178)。

传给 DeepSeek 的核心字段包括：

- 股票基础信息：
  - `code`
  - `name`
  - `market`
  - `market_label`
- 实时量价信息：
  - `price`
  - `pct_chg`
  - `turnover`
  - `volume_ratio`
  - `sixty_day_pct`
  - `ytd_pct`
- 本地综合预测：
  - `prediction.label`
  - `prediction.direction`
  - `prediction.score`
  - `prediction.confidence`
  - `prediction.risk_level`
  - `prediction.advice`
- 短周期结论：
  - `short_horizon`
- 市场环境：
  - `market_regime`
- 证据和反证：
  - `strategy_hits`
  - `missed_strategies`
  - `risk_flags`

### 5.6 DeepSeek 输出什么

当前 prompt 要求输出这些字段：

- `summary`
- `stance`
- `bias`
- `timing`
- `reasoning`
- `entry_plan`
- `risk_controls`
- `strategy_adjustments`
- `avoid_conditions`

其中 `stance` 只允许：

- `buy_trial`
- `watch_only`
- `hold_or_wait`
- `avoid_chase`

前端会映射成：

- `buy_trial` -> `小仓试单`
- `watch_only` -> `只观察`
- `hold_or_wait` -> `等确认`
- `avoid_chase` -> `不追价`

### 5.7 缓存、超时、降级

个股优化有自己的缓存、超时和 fallback：

- 按压缩 payload 计算 `cache_key`
- 命中缓存时直接返回 `cache_hit`
- 超时时间上限为 6 秒
- 遇到 429 / 500 / 502 / 503 / 504 会按配置重试
- 解析失败或超时则返回降级状态

降级时，`optimization` 可能变成：

- `runtime_disabled`
- `disabled`
- `missing_api_key`
- `strategy_not_enabled`
- `timeout`
- `fallback`

## 6. 自动保存与回溯

默认配置：

- `VALIDATION_AUTO_UPDATE_START_TIME=14:30`
- `VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=600`
- `VALIDATION_AUTO_SNAPSHOT_TIME=15:00`
- `VALIDATION_CLOSE_ANCHOR_TIME=15:00`

运行逻辑：

1. 14:30 后自动保存三类推荐快照
2. 每次保存会覆盖同一天同策略的旧批次，只保留当天最后一次
3. 如果某策略当天没有合格股票，也保存为空批次
4. 15:00 后保存必须使用真实收盘锚点；锚点不完整则拒绝保存
5. 保存成功后自动备份验证数据库

## 7. 判断策略是否靠谱

不要只看一天，也不要只看推荐数量。

优先看：

- 真实前瞻样本数量是否够
- 有效样本是否已回填完成
- 次日或主周期胜率是否持续高于 50%
- 平均收益是否扣除成本后仍为正
- 行业是否过度集中
- 空推荐是否来自合理风控，而不是数据缺失

样本不足时，策略只能观察；DeepSeek 建议也只能作为影子调参记录。

---

## 8. 后续优化路线

本节是规划项，不等于当前代码已经全部实现。

阅读口径：

- 前面 1-7 节描述当前实现。
- 本节描述后续优化方向。
- 如果某项尚未在代码中出现，以这里的规划表述为准，不应当理解为“已经上线”。
- 如果某项标注为“已基本落地”，表示当前代码已有对应能力，但仍可能存在体验、参数或稳定性上的继续优化空间。
- 如果某项标注为“尚未系统完成”或“尚未开始”，表示它仍然是路线图，而不是已交付能力。

本路线只覆盖当前三类荐股策略：今天推荐、明天推荐、2-5 天推荐。其他历史页面不作为当前优化目标。

### 8.1 目标

目标不是继续增加榜单，而是让三类推荐更可验证、更少追高、更少行业扎堆，并用真实样本持续迭代。

核心闭环：

```text
本地策略生成候选
  -> DeepSeek 复核风险和排序
  -> 保存当天三类推荐快照
  -> 回填真实行情结果
  -> 策略验证统计表现
  -> DeepSeek 生成影子调参建议
  -> 人工确认后再改正式策略
```

### 8.2 可借鉴思路

| 来源 | 当前只借鉴的点 |
|---|---|
| Qlib | 因子快照、样本外验证、训练/验证/回测分层 |
| vn.py | 数据、策略、风控、展示分层 |
| Backtrader / RQAlpha | 统一成本、滑点、持有期和分析器口径 |
| QUANTAXIS | 本地数据仓和定时任务 |
| FinRL | train-test-trade 流程和 turbulence 风险思想，不优先引入 RL |
| TradingAgents | 研究、交易、风控、组合经理式分层判断 |
| UZI-Skill | 结构化证据、数据覆盖门控、共识极化 |
| Sequoia / myhhub/stock / QuantsPlaybook | A 股日线量价、反转、低波、换手和趋势因子 |
| Qbot | 风控和暴露控制讨论 |

这些仓库只作为方法参考，不复制代码。

### 8.3 路线状态

`已基本落地，继续巩固`

`P0：验证库稳定`

- 只保留三类策略验证数据。
- 空批次也保存，避免页面误显示昨日数据。
- 同一天同策略只保留最后一次批次。
- 查询默认先轻量加载日期，再异步加载指标和 DeepSeek 复盘。

`已基本落地，继续观察`

`P1：DeepSeek 复盘可用`

- 每天 15:00 后自动运行一次复盘。
- 策略验证页支持手动生成复盘。
- 复盘输出问题、样本门控、行业集中、过热风险和影子调参建议。
- 建议只保存，不自动应用。

`尚未系统完成，属于下一阶段优化`

`P2：三策略分开优化`

- 今天推荐：减少高量比、高换手、高涨幅追高样本。
- 明天推荐：强化收盘承接、买入安全和行业分散。
- 2-5 天推荐：强化不过热、趋势延续和波动控制。

`尚未开始，属于后续规划`

`P3：样本足够后再模型化`

当真实前瞻样本足够后，再考虑轻量模型：

- 明天推荐优先做 `model_1d`。
- 2-5 天推荐做 `model_2_5d`。
- 今天推荐先保持规则策略和 DeepSeek 复核，不急于模型化。

模型必须样本外优于现有规则才允许上线。

### 8.4 不做事项

- 不做实盘下单。
- 不做模拟组合入口。
- 不做全市场分钟级训练。
- 不优先做强化学习。
- 不恢复无关隐藏页面。
- 不让 DeepSeek 自动改正式权重。

### 8.5 验收标准

- 策略验证页切换今天、明天、2-5 天不卡顿。
- 每天能看到三类策略当天最后一次保存批次。
- 0 推荐显示为空批次，不串到旧日期。
- 胜率、涨跌个数、平均表现都来自数据库回填结果。
- DeepSeek 复盘每天最多自动生成一次，可手动刷新。
- 调参建议明确标记为影子建议。

## 9. 常见坑

- 不要把“DeepSeek 候选 rerank”和“个股预测优化建议”混为一条链路。前者服务三类荐股，后者服务单只股票分析。
- `short_term`、`tomorrow_picks`、`swing_picks` 的策略周期不同，DeepSeek 复核上下文也不同；调 prompt 或解释结果时不要混用。
- 明天推荐和 2-5 天推荐在页面上可能先显示最近保存结果或空占位，这不等于策略本身没跑出来，要结合 `fallback` / `snapshot.source` 看。
- 个股预测结果里的 `optimization` 是增强层，不是本地预测主结论；DeepSeek 超时或禁用时，仍应以本地 `prediction` 为主。
- 文档里的“后续优化路线”是规划，不等于当前已经落地；判断现状时只看前面 `1-7` 节。

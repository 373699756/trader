# 荐股策略优化计划

> 本计划只覆盖提升荐股胜率、收益质量和风险过滤能力；暂不处理模拟执行、实盘下单、账户同步和券商接口。

## 1. 目标

当前系统已经具备多策略打分、市场状态、风险解释、验证样本和前端看板。下一阶段的目标不是继续堆更多榜单，而是把荐股流程升级为可复现、可验证、可迭代的本地量化研究流程：

```text
本地 A 股数据仓
  -> 每日因子快照
  -> 历史收益标签
  -> 轻量排序模型
  -> 规则风控硬过滤
  -> 多策略 Meta-Ranker
  -> 页面输出：候选股 + 原因 + 风险 + 置信度
```

本机约束：4 核 i5-10210U、约 3.3GB 内存、可用内存偏紧。适合 SQLite + pandas 批处理 + LightGBM/Logistic/Lasso，不适合全市场分钟级训练、深度学习或强化学习。

## 2. GitHub 参考路径

| 仓库 | 路径 | 借鉴点 |
|---|---|---|
| Microsoft Qlib | https://github.com/microsoft/qlib | AI-oriented 量化投研流程、数据集规范、Alpha158/Alpha360、模型训练和回测一体化 |
| VeighNa / vn.py | https://github.com/vnpy/vnpy | 国内量化交易框架、因子工程、模型投研、风控和 Web 推送分层 |
| Backtrader | https://github.com/mementum/backtrader | 事件驱动回测、commission/slippage/analyzer 分层 |
| RQAlpha | https://github.com/ricequant/rqalpha | 数据、算法交易、回测、模拟、实盘和分析的可扩展框架设计 |
| QUANTAXIS | https://github.com/QUANTAXIS/QUANTAXIS | 本地数据仓、任务调度、回测、可视化和多账户架构 |
| AKShare | https://github.com/akfamily/akshare | A 股公开数据接口；当前项目已依赖 |
| Tushare | https://github.com/waditu/tushare | A 股历史、实时、财务、公告和分笔数据补充；当前项目可选依赖 |
| FinRL | https://github.com/AI4Finance-Foundation/FinRL | train-test-trade 流程、技术指标、turbulence 风险指标；只借鉴流程，不优先引入 RL |
| AbuQuant | https://github.com/bbfamily/abu | 股票/期货/期权/机器学习策略样例、形态信号和多市场研究组织 |
| easytrader | https://github.com/shidenggui/easytrader | 未来如需执行层，可借鉴同花顺、miniQMT、雪球组合对接；本阶段不做 |
| TradingAgents | https://github.com/TauricResearch/TradingAgents | 多角色投研分工；当前已映射成本地确定性 `agent_committee` |
| AlphaTrader | https://github.com/14H034160212/AlphaTrader | Serenity / chokepoint 供应链瓶颈方法论 |
| UZI-Skill | https://github.com/wbh604/UZI-Skill | 结构化证据、数据覆盖门控和共识极化 |
| Sequoia | https://github.com/sngyai/Sequoia | 日线量价、趋势和突破类技术选股思路 |
| myhhub/stock | https://github.com/myhhub/stock | A 股数据处理和技术指标选股思路 |
| QuantsPlaybook | https://github.com/hugo2046/QuantsPlaybook | A 股因子复现、反转、低波、换手等研究思路 |
| Qbot | https://github.com/UFund-Me/Qbot | 小市值因子、组合和风控讨论 |

## 3. 阶段计划

### 阶段 1：全市场日线数据仓

目标：把实时荐股依赖从临时接口调用转到本地可复现数据仓。

任务：

- 扩充 `.runtime/market_data.sqlite3` 到沪深主板、创业板、科创板全市场。
- 保存 3-5 年日线：前复权价格、不复权价格、成交量、成交额、换手率、流通市值、PE、PB。
- 增加股票状态：ST、退市、停牌、新股上市天数、涨跌停价格。
- 每日收盘后增量更新；页面只读缓存，不在前台批量拉远程历史。

验收：

- 全市场日线覆盖率大于 95%。
- 任意历史交易日能还原当日可交易股票池。
- 回测不依赖实时行情接口。

### 阶段 2：因子快照表

目标：把当前散落在策略里的量价计算沉淀成可横向比较的因子库。

任务：

- 新增 `factor_snapshots` 表，按 `trade_date + code` 保存因子。
- 第一版控制在 50-80 个低成本因子：
  - 动量：1/3/5/10/20/60 日收益。
  - 反转：短期涨幅过热、冲高回落、长上影。
  - 趋势：MA5/10/20/60 偏离、均线多头、20 日新高。
  - 波动：20 日波动率、平均振幅、上下影线。
  - 量价：量比、成交额放大、换手变化。
  - 流动性：成交额、换手率、流通市值。
  - 估值质量：PE、PB、ROE、毛利率；缺失回到中性。
  - 风险：涨停附近、高换手、高振幅、长上影、流动性不足。
  - 市场状态：上涨家数、中位涨幅、强弱股比例、市场波动。
- 对因子做 winsorize、z-score、缺失值中性处理。

验收：

- 每个因子都有覆盖率。
- 每个因子能输出 IC、RankIC、分组收益。
- 页面推荐不再重复计算大量历史因子。

### 阶段 3：历史收益标签和统一回测

目标：让所有策略使用同一套扣成本、可交易约束的评价口径。

任务：

- 新增 `return_labels` 表：
  - 次日开盘到收盘净收益。
  - 5 日净收益。
  - 10 日净收益。
  - 20 日净收益。
  - 相对指数或行业超额收益。
- 统一交易约束：T+1、涨停买不进、跌停卖不出、停牌不可交易、佣金、滑点、冲击成本。
- 将现有 `strategy_validation` 扩展为统一评价系统。

验收：

- 每个策略都有净值曲线、胜率、平均收益、最大回撤、盈亏比、换手。
- 所有指标都扣成本。
- `tomorrow_picks`、`swing_picks`、`breakout_picks`、`reversal_picks` 可在同一口径下比较。

### 阶段 4：轻量排序模型

目标：用样本外验证过的模型分替代部分手写权重，而不是直接堆规则。

任务：

- 第一优先级：`model_1d`，服务明天预测。
- 第二优先级：`model_5d_10d`，服务波段和突破。
- 第三优先级：`model_20d`，服务中长期、科技潜力和卡脖子。
- 首选 LightGBM Ranker；若依赖暂不引入，先用 Logistic/Ridge/Lasso 做 baseline。
- 使用 walk-forward：训练 18-36 个月，验证 1-3 个月，按交易日分组，不做随机切分。

验收：

- 样本外扣成本收益优于现有规则。
- Top5、Top10、Top30 都有对比。
- 如果模型不优于规则，不上线，只保留报告。

### 阶段 5：Meta-Ranker 融合

目标：让现有策略成为候选源和解释源，最终排序由模型、共识、市场状态和风控共同决定。

建议最终分数：

```text
final_score =
  model_score * 0.45
+ consensus_score * 0.20
+ market_regime_score * 0.10
+ strategy_validation_score * 0.10
+ quality_score * 0.10
+ theme_score * 0.05
- risk_penalty
```

任务：

- 保留 `tomorrow_picks`、`swing_picks`、`breakout_picks`、`reversal_picks`、`position_picks` 作为候选源。
- `tech_potential` 和 `chokepoint_picks` 作为主题增强，不单独主导每日主榜。
- 计算策略间信号相关性，高度同源的策略降权，避免动量类信号重复投票。

验收：

- 每只推荐股能解释模型分、共识分、风控扣分、策略来源。
- 同源策略不会把同一类追涨信号重复放大。
- 融合榜单样本外优于任一单策略。

### 阶段 6：风控和策略淘汰

目标：先少踩坑，再追求更高收益。

任务：

- 硬过滤：ST、退市、停牌、新股天数不足、成交额不足、涨停附近不可买、跌停/一字板、极端高换手、高振幅、长上影。
- 事件风险：解禁、减持、质押、财报窗口。
- 长期黑名单：财务造假、重大违法、重大处罚、审计无法表示意见、重大内控缺陷、长期严重负面信息，维护在 `.runtime/risk_blacklist.json` 或 `.runtime/risk_blacklist.csv`。
- 策略淘汰：最近 N 个真实样本低于基准自动降权；连续样本外亏损退出共识。
- 偏防守市自动减少追涨、突破、题材类策略权重。

验收：

- 页面显示被剔除或降级的原因。
- 亏钱策略不会继续高权重参与共识。
- 防守市下主榜明显减少高追涨风险票。

### 阶段 7：页面决策升级

目标：页面从榜单展示升级成荐股决策台。

任务：

- 首页展示市场环境、可交易候选数量、主榜 Top10、高风险剔除数量、模型样本外表现。
- 个股详情展示预期周期、模型分、共识来源、风控扣分、同类历史样本胜率和建议动作。
- 所有推荐可追溯到样本、因子、策略和风控理由。

验收：

- 用户能看到为什么推荐、为什么降级、历史同类表现如何。
- 每条推荐都有数据依据，而不是单纯分数。

## 4. 执行优先级

1. 全市场日线数据仓。
2. 因子快照表。
3. 收益标签和统一回测。
4. 明天预测模型化。
5. 波段/突破模型化。
6. Meta-Ranker 融合。
7. 策略淘汰和风控页面。

## 5. 第一批落地任务

第一批只做明天预测模型化所需的最小闭环：

1. 扩充全市场日线缓存。
2. 生成 50 个以内的明天预测因子。
3. 生成次日开盘到收盘扣成本收益标签。
4. 训练 Logistic baseline，后续再换 LightGBM Ranker。
5. 输出对比报告：现有 `tomorrow_picks` vs 模型 TopN。
6. 样本外胜出后，将 `model_score_1d` 接入 `tomorrow_picks` 和首页共识榜。

## 6. 不做事项

- 不做实盘下单。
- 不做模拟执行。
- 不做全市场分钟级训练。
- 不做重型深度学习或强化学习优先上线。
- 不把模型分当成确定性预测，必须保留风控硬过滤和样本外验证。

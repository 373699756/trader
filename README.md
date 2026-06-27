# A 股实时强势股推荐看板

本项目是一个本地 Flask 看板，用 AKShare 拉取 A 股实时行情、热度和新闻舆情，按短线强势逻辑给沪深主板、创业板、科创板股票打分并推荐 Top N。

结果仅供研究，不构成投资建议。

> **关于"能不能赚钱"**：任何策略都**不保证盈利**。新增的反转/低波/小市值/量价突破策略是统计上有正期望的因子，但回测≠实盘，小市值更有 2024 微盘崩盘级尾风险。本系统的价值是把多策略放进**回测验证闭环**用数据判断可信度，而非保证收益。详见 [`docs/strategies.md`](docs/strategies.md)。

## 功能

- 支持主板、创业板、科创板，默认排除北交所、ST、退市、停牌和低流动性股票。
- 每 30 秒刷新短期 10 支和长期 10 支推荐列表。
- 短期评分偏行情动能、量价强度、人气热度和实时舆情。
- 长期评分偏 60 日/YTD 趋势、流动性、行业强度、舆情质量和风险过滤。
- AlphaLite 历史因子增强：3/5/10/20 日动量、均线偏离、成交额放大、20 日突破、20 日波动率。
- 市场状态识别：根据候选池涨跌广度、中位涨幅、强弱占比和振幅，区分偏进攻 / 均衡 / 偏防守环境。
- 多策略共识榜：汇总短线、长线、明天预测、科技潜力、波段、中长期 6 套策略的重复入选股票。
- Serenity Profile：结合 Serenity/chokepoint 瓶颈投资方法论、UZI-Skill 的结构化证据和当前本地因子，为每条推荐生成质量分、风险分、置信度、证据和动作建议。
- TradingAgents 委员会：参考 `TauricResearch/TradingAgents` 的分析师、牛熊研究、交易员、风控和组合经理分层决策流，为每条推荐生成 `agent_committee`。
- Verdict 评级阶梯：把裸 0-100 综合分映射为强烈关注/关注/观察/谨慎/回避，数据覆盖不足时强制降级并标注（参考 UZI-Skill / Buffett 评级思路）。
- 多空双分：每条推荐顶层暴露 `bull_score` 与 `bear_score`，区分“强但过热”和“平庸”，前端以双进度条展示。
- 过热乘法抑制：对短线/明天/科技/波段的最终分施加 `_not_overextended_score` 派生的乘法 damp，完全过热的票无法靠动量挤进前列。
- 市况自适应权重：偏进攻市自动放大动量/趋势/突破/量能因子，偏防守市自动放大低波/质量/流动性因子；结果行输出 `regime_weight_profile` 解释当日权重画像。
- 卡脖子主题倾斜：科技潜力策略加入 `_chokepoint_score`，奖励供给紧、尚未被重定价的上游/元件环节（Serenity / chokepoint 方法论）。
- 卡脖子独立策略页：`/api/chokepoint-picks` 以 `_chokepoint_score` 为主导因子，只保留命中上游环节的票并按产业链环节（半导体设备/材料、封装/载板、EDA/IP、光器件、高端材料、精密零部件）归类，meta 附 `chain` 用于前端产业链全景图。
- 反转低波策略（`/api/reversal-picks`）：依据 A 股短线反转+低波动+高换手回避证据，挑超跌且不躁动的标的，含接飞刀风险惩罚。
- 小市值价值策略（`/api/smallcap-value-picks`）：复用东方财富已到位的流通市值/PE/PB，低市值+低估值，含市值下限、亏损过滤、流动性与防守降权护栏（注意 2024 微盘崩盘尾风险）。
- 量价突破策略（`/api/breakout-picks`）：均线多头排列（MA5>MA10>MA20>MA60）或 20 日新高 + 量能突破的趋势确认型选股。
- 动量→反转修正开关：`short_term` 新增可回测的 `reversal_tilt`（默认关闭），用 `python -m stock_analyzer.calibrate --compare-momentum` 由回测决定是否启用及幅度。
- 共识极化与可信度：多策略共识按跨策略分歧度做极化拉伸（一致拉伸、分歧压缩），并以各策略主周期净胜率作为可信度乘子，真实前瞻样本优先于历史回放样本。
- 回测权重校准：`python -m stock_analyzer.calibrate` 以滚动回测胜率+平均收益为目标离线扫描 AlphaLite 信号权重，写入 `.runtime/weights.json` 供运行时覆盖。
- TopK-Dropout 稳定榜单：展示新进、留存、连续上榜次数，减少刷新噪声。
- SQLite 历史 K 线缓存：减少重复请求免费行情接口。
- 止损/持有期退出模拟：策略验证和 AlphaLite 回测统一使用固定持有期 + 固定止损 + 止盈 + 移动止损，不再只按期末收盘价评估。
- 滚动回测：按 AlphaLite 信号做多期 TopK 组合验证，输出胜率、累计收益、最大回撤、扣成本收益和退出原因。
- 点击股票可查看相关新闻、电报、关键词命中和舆情分。
- 行情优先使用 AKShare；配置 `TUSHARE_TOKEN` 后可尝试 Tushare 降级。

## 已落实的 Web 优化

- 左侧模块导航：把原顶部 Tab 改成固定工作台侧栏，按决策台、今日推荐、明天预测、波段/中长期、科技潜力、策略验证和策略说明组织。
- 决策台首页：新增市场状态、操作建议、强共识数量、优先跟踪数量和共识风险均值，先给交易前判断，再进入个股。
- 全局动作筛选和排序：支持按优先跟踪、小仓观察、等待确认、只观察过滤，并支持默认排名、质量、风险、综合分、成交额排序。
- 右侧股票详情抽屉：点击任意股票先展示本地评分证据、质量/风险/置信度、共识来源和风险标签，再加载舆情新闻。
- 策略验证工作台：按“保存预测 -> 选择样本 -> 更新结果 -> 查看批次明细”重排，降低操作路径复杂度。
- 历史数据回填验证：策略验证页支持“下载历史并验证”，会先把已保存预测股票的日线写入 `.runtime/history_cache.sqlite3`，再批量更新验证结果。
- ECharts 图表可视化：决策台新增市场环境仪表盘与共识热度散点，详情抽屉新增策略子分雷达，策略验证页新增各策略主周期净胜率走势折线（消费 `/api/validation-overview`）；图表库离线不可用时优雅降级为提示文字。
- 评级直观化：各策略表格综合分列改为 Verdict 评级徽章 + 数值，详情抽屉以多空双进度条呈现 `bull_score` / `bear_score`。
- 现代深色仪表盘：整站改为深色主题（颜色全部收敛进 `:root` 语义变量 + ECharts 统一 `CHART_THEME`），保留 A 股红涨绿跌约定。
- 卡脖子页：侧栏新增「卡脖子」tab，顶部按产业链环节展示代表票卡片（产业链全景图），下方为实时打分榜。
- 策略验证页重做：顶部「记分牌」给每个策略一眼结论（主周期净胜率徽章 + 一句话可信度判断），左侧单按钮操作面板（保存预测 / 下载历史并验证）带就地成功/失败/进度反馈，右侧复盘区自动选中最新批次。

## 推荐策略

默认推荐使用“强共识 + Agent 风控优先策略”，适合作为本项目的主工作流：

1. 先看“决策台”的市场状态。如果市场状态偏防守，只观察或降低仓位，不因为个股高分直接追高。
2. 在“多策略共识”里优先选择 2 个以上策略同时入选的股票，动作筛选选“优先跟踪”。
3. 风险控制优先于分数排序：优先看 `avg_risk <= 55`、`avg_quality >= 60`、`avg_agent_score >= 56`，并避开 Agent 委员会给出“风控否决”的股票。
4. 如果同一股票同时出现在明天预测、波段、科技潜力或短期推荐中，且 Serenity Profile 给出较高质量分、较低风险分，TradingAgents 委员会给出“组合经理批准”或“交易员小仓试单”，再进入人工复核。
5. 每天收盘后保存预测，次日或后续交易日用“策略验证”更新结果。若 20 个保存日内样本不足，不把命中率当成稳定结论；需要快速补足样本时，先用“回放历史补样本”做离线粗筛，再继续积累真实前瞻样本。

该策略不保证收益，只是把多策略重复确认、风险优先和验证闭环放到同一个操作流程里。

## TradingAgents 策略映射

本项目没有复制或运行 `TauricResearch/TradingAgents` 的代码，也没有默认接入 LLM。当前采用确定性映射：

- 技术分析师：读取动量、趋势、均线偏离、突破、执行性等字段。
- 情绪分析师：读取舆情分和风险关键词。
- 基本面代理：由于当前未接入财务/估值，使用行业强度、主题、流动性、不过热分作为代理。
- 新闻/市场环境：读取市场状态 `market_regime` 和策略顺逆风加减分。
- 牛方研究员：汇总技术、情绪、流动性、主题和市场顺风证据。
- 熊方研究员：汇总追高、透支、负面舆情、波动和流动性风险。
- 交易员：把牛方分、熊方风险和市场环境转成可执行分。
- 风控经理：对高涨幅、高量比、高换手、高振幅、涨幅透支和负面舆情做否决或压分。
- 组合经理：生成最终 `final_score`、`stance` 和 `final_action_label`，并反向影响 Serenity Profile 和多策略共识排序。

## 开源参考

本工程直接运行依赖的主要开源库：

- [Flask](https://github.com/pallets/flask)：Web 服务和 API 路由。
- [pandas](https://github.com/pandas-dev/pandas)：行情数据清洗、排序和表格计算。
- [NumPy](https://github.com/numpy/numpy)：数值计算。
- [requests](https://github.com/psf/requests)：HTTP 请求。
- [AKShare](https://github.com/akfamily/akshare)：A 股实时行情、历史行情和公开数据源。
- [Tushare](https://github.com/waditu/tushare)：可选降级行情源，需要配置 `TUSHARE_TOKEN`。

本轮策略优化参考了以下 GitHub 开源项目和方法论，未复制其代码：

- [14H034160212/AlphaTrader](https://github.com/14H034160212/AlphaTrader)：参考 Serenity / chokepoint 投资方法论，上溯供应链瓶颈环节，偏向供给紧、难替代、尚未被充分重定价的上游主题。
- [wbh604/UZI-Skill](https://github.com/wbh604/UZI-Skill)：参考结构化证据、数据覆盖自检门控和共识极化拉伸。
- [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)：参考多智能体投研分工、牛熊辩论、交易员、风控和组合经理审批流程，映射为本地 `agent_committee` 确定性评分。

## 启动

一键运行：

```bash
chmod +x run.sh
./run.sh
```

默认打开 `http://127.0.0.1:5000`。可用环境变量改端口：

```bash
PORT=5050 ./run.sh
```

手动运行：

依赖当前 AKShare 版本，需要 Python 3.9 及以上；推荐 Python 3.11。若已有旧的 Python 3.8 虚拟环境，请先删除后重建。

```bash
rm -rf .venv .venc
/home/c/.local/bin/python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:5000`。

## 可选配置

```bash
export TUSHARE_TOKEN=你的token
export REFRESH_SECONDS=30
export DEFAULT_TOP_N=20
export MIN_TURNOVER=50000000
export MAX_RECOMMENDED_GAIN=18.5
export HISTORY_FACTOR_LIMIT=40
export HISTORY_CACHE_PATH=.runtime/history_cache.sqlite3
export HISTORY_CACHE_FRESHNESS_HOURS=18
export VALIDATION_TRADE_COST_PCT=0.25
export EXIT_STOP_LOSS_PCT=5.0
export EXIT_TAKE_PROFIT_PCT=8.0
export EXIT_TRAILING_STOP_PCT=4.0
```

## 接口

- `GET /api/recommendations?top_n=10&market=all`
- `GET /api/sentiment/<code>?name=<股票名>`
- `GET /api/backtest?codes=600000,000001&top_k=10&holding_days=3&mode=rolling`
- `GET /api/backtest?codes=600000,000001&top_k=10&holding_days=3&mode=snapshot`
- `POST /api/strategy-validation/prefetch-history?strategy=tomorrow_picks&date=2024-01-01&days=180&update=1`
- `GET /api/reversal-picks?top_n=30&market=all`
- `GET /api/smallcap-value-picks?top_n=30&market=all`
- `GET /api/breakout-picks?top_n=30&market=all`
- `POST /api/strategy-validation/backfill-samples?strategy=tomorrow_picks&days=260&replay_days=20&top_n=30`
- `GET /api/health`

`market` 可选值：`all`、`main`、`chinext`、`star`。

`/api/recommendations` 会返回：

- `recommendations.short_term`：短期 Top 10。
- `recommendations.long_term`：长期 Top 10。
- `data`：兼容字段，等同于短期 Top 10。
- `meta.market_regime`：当前市场状态、广度、强弱分布和操作建议。
- `recommendations.*[].serenity_profile`：单票质量、风险、置信度、证据和动作建议。
- `recommendations.*[].agent_committee`：TradingAgents 风格委员会结论，包含技术/情绪/基本面代理/新闻环境/牛熊/交易员/风控/组合经理分数和最终动作。
- `meta.strategy_consensus.rows`：多策略重复入选的高共识标的，包含质量、风险、共识分和动作建议。
- `meta.strategy_consensus.serenity_references`：当前 Serenity/chokepoint 与 UZI-Skill 参考来源和对应借鉴点。
- `meta.strategy_consensus.trading_agents_reference`：本轮策略优化参考的 TradingAgents 仓库和借鉴点。

运行时会创建 `.runtime/recommendation_state.json` 保存稳定榜状态。

运行时会创建 `.runtime/history_cache.sqlite3` 保存日线历史数据缓存；策略验证的“下载历史并验证”和“回放历史补样本”都会复用这个数据库，避免每次复盘都重新请求远程行情。

## 样本不足处理

策略验证的“样本不足”指验证库里已经有结果、且已经走完该策略主周期的信号少于 30 条，不是单纯缺少K线。只下载历史K线只能更新已保存信号的结果，不能凭空增加过去的推荐样本。

当前主评估口径是“主周期净收益/净胜率”，并会扣 `VALIDATION_TRADE_COST_PCT`（默认 0.25%）作为固定交易成本/滑点近似。不同策略的主周期不同：明天预测/短期看次日，反转低波看 5 日，波段/突破看 10 日，中长期/科技/卡脖子/小市值价值看 20 日。未来交易日不足时，该样本只计入 `outcome_sample_count`，不计入主样本 `sample_count`。

策略验证还会额外记录 `signal_exit_return` / `exit_reason` / `exit_days` / `exit_date`：按主周期窗口内的止损、止盈、移动止损或持有到期计算退出收益。默认参数是 5% 固定止损、8% 止盈、4% 移动止损，可用 `EXIT_STOP_LOSS_PCT`、`EXIT_TAKE_PROFIT_PCT`、`EXIT_TRAILING_STOP_PCT` 调整。该模型仍是日线近似：同一天同时触发止损和止盈时按保守顺序先算止损，且未精确模拟涨跌停不可成交和盘中滑点。

当前提供两条路径：

1. 真实样本：每天在盘后保存推荐，次日或后续交易日点击“下载历史并验证”/“仅更新当前批次”。这是最可信的前瞻记录。
2. 离线补样本：点击“回放历史补样本”，系统会下载/复用日线历史，用当前量价规则模拟过去若干个交易日的信号，写入 `*_replay_v1` 版本并立即计算结果。该结果用于快速判断规则是否值得继续跟踪，不等同于真实历史曾经推荐过。

默认回放参数为近 260 日历史、回放 20 个历史交易日、每个交易日保留 Top 30。回放样本仍归入原策略名称统计，因此可以快速把 `tomorrow_picks`、`swing_picks` 等策略从“样本不足”推进到可观察状态；同时明细里的 `strategy_version` 会标记为 `tomorrow_picks_replay_v1` 这类版本，便于和真实保存样本区分。共识权重优先采信真实前瞻样本；真实样本不足时只会轻度参考回放，避免回放结果虚高。

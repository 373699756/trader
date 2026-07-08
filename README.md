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
- 多策略共识榜：汇总短线、长线、明天预测、科技潜力、波段、中长期 6 套实时主策略的重复入选股票，保留 Top 30；卡脖子、反转低波、小市值价值、量价突破作为独立策略和验证对象。
- Serenity Profile：结合 Serenity/chokepoint 瓶颈投资方法论、UZI-Skill 的结构化证据和当前本地因子，为每条推荐生成质量分、风险分、置信度、证据和动作建议。
- TradingAgents 委员会：参考 `TauricResearch/TradingAgents` 的分析师、牛熊研究、交易员、风控和组合经理分层决策流，为每条推荐生成 `agent_committee`。
- Verdict 评级阶梯：把裸 0-100 综合分映射为强烈关注/关注/观察/谨慎/回避，数据覆盖不足时强制降级并标注（参考 UZI-Skill / Buffett 评级思路）。
- 多空双分：每条推荐顶层暴露 `bull_score` 与 `bear_score`，区分“强但过热”和“平庸”，前端以双进度条展示。
- 过热乘法抑制：对短线/明天/科技/波段的最终分施加 `_not_overextended_score` 派生的乘法 damp，完全过热的票无法靠动量挤进前列。
- 市况自适应权重：偏进攻市自动放大动量/趋势/突破/量能因子，偏防守市自动放大低波/质量/流动性因子；结果行输出 `regime_weight_profile` 解释当日权重画像。
- 卡脖子主题倾斜：科技潜力策略加入 `_chokepoint_score`，奖励供给紧、尚未被重定价的上游/元件环节（Serenity / chokepoint 方法论）。
- 卡脖子独立策略页：`/api/chokepoint-picks` 以 `_chokepoint_score` 为主导因子，只保留命中上游环节的票，并按先进光刻、半导体设备/材料、国产算力、玻璃基板、先进封装、工业软件、低轨星座等产业链环节归类；完整行业口径见 [`docs/strategies.md`](docs/strategies.md)。
- 反转低波策略（`/api/reversal-picks`）：依据 A 股短线反转+低波动+高换手回避证据，挑超跌且不躁动的标的，含接飞刀风险惩罚。
- 小市值价值策略（`/api/smallcap-value-picks`）：复用东方财富已到位的流通市值/PE/PB，低市值+低估值，含市值下限、亏损过滤、流动性与防守降权护栏（注意 2024 微盘崩盘尾风险）。
- 量价突破策略（`/api/breakout-picks`）：均线多头排列（MA5>MA10>MA20>MA60）或 20 日新高 + 量能突破的趋势确认型选股。
- 动量→反转修正开关：`short_term` 新增可回测的 `reversal_tilt`（默认关闭），用 `python -m stock_analyzer.calibrate --compare-momentum` 由回测决定是否启用及幅度。
- 共识极化与可信度：多策略共识按跨策略分歧度做极化拉伸（一致拉伸、分歧压缩），并以各策略主周期净胜率作为可信度乘子，真实前瞻样本优先于历史回放样本。
- AlphaLite 回测权重校准：`python -m stock_analyzer.calibrate` 以滚动回测胜率+平均收益为目标离线扫描 AlphaLite 信号权重。
- 上线策略权重校准：`python -m stock_analyzer.calibrate --calibrate-live-weights all --dry-run` 直接读取真实验证样本，重算 `score_*_candidates` 的声明式权重，满足样本和改善阈值后写入 `.runtime/weights.json`。
- TopK-Dropout 稳定榜单：展示新进、留存、连续上榜次数，减少刷新噪声。
- SQLite 历史 K 线缓存：减少重复请求免费行情接口。
- 止损/持有期退出模拟：策略验证和 AlphaLite 回测统一使用固定持有期 + 固定止损 + 止盈 + 移动止损，不再只按期末收盘价评估。
- 滚动回测：按 AlphaLite 信号做多期 TopK 组合验证，输出胜率、累计收益、最大回撤、扣成本收益和退出原因。
- 事件风险层：可选接入解禁、质押、减持和财报窗口风险，默认关闭；开启后高风险票会提高风险分、降级动作，必要时从硬过滤中剔除。
- 组合总仓控制：组合约束能力保留在后端，当前 Web 入口暂时隐藏；需要恢复时可重新打开组合约束页。
- 基本面/因子 IC 框架：可选接入 ROE、毛利率、资产负债率、估值、业绩超预期和评级调整；`daily_job` 会基于真实样本刷新因子 IC，用来识别长期无效因子。
- 点击股票可查看相关新闻、电报、关键词命中和舆情分。
- 行情优先使用 AKShare；配置 `TUSHARE_TOKEN` 后可尝试 Tushare 降级。

## 已落实的 Web 优化

- 左侧模块导航：把原顶部 Tab 改成固定工作台侧栏，按决策台、今日推荐、明天预测、波段/中长期、科技潜力、策略验证和策略说明组织。
- 决策台首页：新增市场状态、操作建议、强共识数量、优先跟踪数量和共识风险均值，先给交易前判断，再进入个股。
- 全局动作筛选和排序：支持按优先跟踪、小仓观察、等待确认、只观察过滤，并支持默认排名、质量、风险、综合分、成交额排序。
- 右侧股票详情抽屉：点击任意股票先展示本地评分证据、质量/风险/置信度、共识来源和风险标签，再加载舆情新闻。
- 策略验证工作台：按“保存预测 -> 后台自动回填 -> 选择样本 -> 查看批次明细”重排，降低操作路径复杂度。
- 历史数据自动回填验证：后台按策略和股票代码分批把已保存预测股票的日线写入 `.runtime/history_cache.sqlite3`，再批量更新验证结果；Web 不再放“下载历史并验证”按钮。
- ECharts 图表可视化：决策台新增市场环境仪表盘与共识热度散点，详情抽屉新增策略子分雷达，策略验证页新增各策略主周期净胜率走势折线（消费 `/api/validation-overview`）；图表库离线不可用时优雅降级为提示文字。
- 评级直观化：各策略表格综合分列改为 Verdict 评级徽章 + 数值，详情抽屉以多空双进度条呈现 `bull_score` / `bear_score`。
- 现代深色仪表盘：整站改为深色主题（颜色全部收敛进 `:root` 语义变量 + ECharts 统一 `CHART_THEME`），保留 A 股红涨绿跌约定。
- 卡脖子页：侧栏新增「卡脖子」tab，顶部按产业链环节展示代表票卡片（产业链全景图），下方为实时打分榜。
- 策略验证页重做：顶部「记分牌」给每个策略一眼结论（主周期净胜率徽章 + 一句话可信度判断），左侧操作面板保留保存、回放和人工复核，历史 K 线下载改由后台自动分批执行，右侧复盘区自动选中最新批次。
- 组合约束能力：基于最近一次保存快照生成建议仓位，加入单票上限、主题/行业上限和现金保留提示；当前 Web 入口暂时隐藏，避免干扰明天预测复盘主流程。

## 推荐策略

默认推荐使用“强共识 + Agent 风控优先策略”，适合作为本项目的主工作流：

1. 先看“决策台”的市场状态。如果市场状态偏防守，只观察或降低仓位，不因为个股高分直接追高。
2. 在“多策略共识”里优先选择 2 个以上策略同时入选的股票，动作筛选选“优先跟踪”。
3. 风险控制优先于分数排序：优先看 `avg_risk <= 55`、`avg_quality >= 60`、`avg_agent_score >= 56`，并避开 Agent 委员会给出“风控否决”的股票。
4. 如果同一股票同时出现在明天预测、波段、科技潜力或短期推荐中，且 Serenity Profile 给出较高质量分、较低风险分，TradingAgents 委员会给出“组合经理批准”或“交易员小仓试单”，再进入人工复核。
5. 每天收盘后保存预测，次日或后续交易日用“策略验证”更新结果。若 20 个保存日内样本不足，不把命中率当成稳定结论；需要快速补足样本时，先用“回放历史补样本”做离线粗筛，再继续积累真实前瞻样本。

该策略不保证收益，只是把多策略重复确认、风险优先和验证闭环放到同一个操作流程里。

完整策略权重、过滤条件、适用市况、空榜原因、单票预测和验证周期见 [`docs/strategies.md`](docs/strategies.md)。

## TradingAgents 策略映射

本项目没有复制或运行 `TauricResearch/TradingAgents` 的代码，也没有默认接入 LLM。当前采用确定性映射：

- 技术分析师：读取动量、趋势、均线偏离、突破、买入安全等字段。
- 情绪分析师：读取舆情分和风险关键词。
- 基本面代理：默认未启用财务/估值接口时，使用行业强度、主题、流动性、不过热分作为代理。
- 基本面增强：开启 `ENABLE_FUNDAMENTALS=1` 后，会把质量、估值、业绩超预期和评级调整作为独立低相关 alpha 源；数据缺失时安全降级为代理分。
- 新闻/市场环境：读取市场状态 `market_regime` 和策略顺逆风加减分。
- 牛方研究员：汇总技术、情绪、流动性、主题和市场顺风证据。
- 熊方研究员：汇总追高、透支、负面舆情、波动和流动性风险。
- 交易员：把牛方分、熊方风险和市场环境转成可执行分。
- 风控经理：对高量比、高换手、高振幅、当日追高和负面舆情做风险压分；阶段涨幅透支由 `overheat_damp` 统一硬门控。
- 组合经理：生成最终 `final_score`、`stance` 和 `final_action_label`，并反向影响 Serenity Profile 和多策略共识排序。

## 开源参考

完整的荐股优化路线图、阶段验收和 GitHub 参考路径见 [`docs/strategy_optimization_plan.md`](docs/strategy_optimization_plan.md)。

本工程直接运行依赖的主要开源库：

- [Flask](https://github.com/pallets/flask)：Web 服务和 API 路由。
- [pandas](https://github.com/pandas-dev/pandas)：行情数据清洗、排序和表格计算。
- [NumPy](https://github.com/numpy/numpy)：数值计算。
- [requests](https://github.com/psf/requests)：HTTP 请求。
- [AKShare](https://github.com/akfamily/akshare)：A 股实时行情、历史行情和公开数据源。
- [Tushare](https://github.com/waditu/tushare)：可选降级行情源，需要配置 `TUSHARE_TOKEN`。

本轮策略优化参考了以下 GitHub 开源项目和方法论，未复制其代码：

- [microsoft/qlib](https://github.com/microsoft/qlib)：参考 AI-oriented 量化投研流程、Alpha158/Alpha360 因子体系、模型训练和回测一体化。
- [vnpy/vnpy](https://github.com/vnpy/vnpy)：参考国内量化交易框架、因子工程、模型投研、风控和 Web 推送分层。
- [mementum/backtrader](https://github.com/mementum/backtrader)：参考事件驱动回测、commission/slippage/analyzer 分层。
- [ricequant/rqalpha](https://github.com/ricequant/rqalpha)：参考可扩展回测、模拟和分析框架设计。
- [QUANTAXIS/QUANTAXIS](https://github.com/QUANTAXIS/QUANTAXIS)：参考本地数据仓、任务调度、回测、可视化和多账户架构。
- [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL)：参考 train-test-trade 流程、技术指标和 turbulence 风险指标；本项目不优先引入 RL。
- [bbfamily/abu](https://github.com/bbfamily/abu)：参考股票/期货/期权/机器学习策略样例、形态信号和多市场研究组织。
- [shidenggui/easytrader](https://github.com/shidenggui/easytrader)：未来如需执行层，可参考同花顺、miniQMT、雪球组合对接；当前暂不做模拟执行和实盘下单。
- [14H034160212/AlphaTrader](https://github.com/14H034160212/AlphaTrader)：参考 Serenity / chokepoint 投资方法论，上溯供应链瓶颈环节，偏向供给紧、难替代、尚未被充分重定价的上游主题。
- [wbh604/UZI-Skill](https://github.com/wbh604/UZI-Skill)：参考结构化证据、数据覆盖自检门控和共识极化拉伸。
- [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)：参考多智能体投研分工、牛熊辩论、交易员、风控和组合经理审批流程，映射为本地 `agent_committee` 确定性评分。
- [sngyai/Sequoia](https://github.com/sngyai/Sequoia)：参考日线量价、趋势和突破类技术选股思路。
- [myhhub/stock](https://github.com/myhhub/stock)：参考 A 股数据处理和技术指标选股思路。
- [hugo2046/QuantsPlaybook](https://github.com/hugo2046/QuantsPlaybook)：参考 A 股因子复现、反转、低波和换手等研究思路。
- [UFund-Me/Qbot](https://github.com/UFund-Me/Qbot)：参考小市值因子、组合和风控讨论。

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

`run.sh` 会在部署层默认启用历史因子增强（`ENABLE_HISTORY_FACTORS=1`），但不修改 `config.py` 默认值；如需降级为纯实时代理，可显式运行：

```bash
ENABLE_HISTORY_FACTORS=0 ./run.sh
```

为避免量价突破等页面被免费行情源卡住，前台请求默认只读取 `.runtime/history_cache.sqlite3` 里的历史缓存，不会在页面加载时批量远程拉 K 线。缓存不足时，量价突破会使用实时涨幅、涨速、量比、成交额和 60 日趋势做兜底筛选。若确实要允许页面请求时少量远程补历史，可显式开启：

```bash
HISTORY_FACTORS_FETCH_ON_REQUEST=1 HISTORY_FACTORS_MAX_REQUEST_FETCHES=8 ./run.sh
```

手动运行：

依赖当前 AKShare/numpy/pandas 版本，需要 Python 3.9-3.11；推荐 Python 3.11。若已有其他版本虚拟环境，请先删除后重建。

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
export DEFAULT_TOP_N=30
export MIN_TURNOVER=50000000
export MAX_RECOMMENDED_GAIN=18.5
export ENABLE_HISTORY_FACTORS=1
export HISTORY_FACTOR_LIMIT=40
export HISTORY_FACTORS_FETCH_ON_REQUEST=0
export HISTORY_FACTORS_MAX_REQUEST_FETCHES=8
export HISTORY_CACHE_PATH=.runtime/history_cache.sqlite3
export HISTORY_CACHE_FRESHNESS_HOURS=18
export VALIDATION_TRADE_COST_PCT=0.25
export VALIDATION_PRIMARY_ENTRY_MODE=open
export VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT=0.05
export VALIDATION_SLIPPAGE_MID_TURNOVER_PCT=0.12
export VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT=0.25
export VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT=0.45
export STRATEGY_DECAY_MIN_REAL_SAMPLES=20
export STRATEGY_DECAY_WIN_RATE_FLOOR=42
export STRATEGY_DECAY_AVG_RETURN_FLOOR=0
export STRATEGY_RETIRE_WINRATE=48
export PORTFOLIO_MAX_POSITIONS=10
export PORTFOLIO_SINGLE_CAP=0.15
export PORTFOLIO_THEME_CAP=0.35
export PORTFOLIO_GROSS_RISK_ON=1.0
export PORTFOLIO_GROSS_BALANCED=0.7
export PORTFOLIO_GROSS_RISK_OFF=0.4
export PORTFOLIO_DD_LEVEL_1=8.0
export PORTFOLIO_DD_FACTOR_1=0.7
export PORTFOLIO_DD_LEVEL_2=15.0
export PORTFOLIO_DD_FACTOR_2=0.4
export PAPER_TRADING_DB_PATH=.runtime/paper_trading.sqlite3
export PAPER_TRADING_HISTORY_DAYS=220
export PAPER_TRADING_SPREAD_CAPITAL_BY_HOLDING_DAYS=1
export ENABLE_EVENT_RISK=0
export EVENT_RISK_HARD_FILTER=0
export EVENT_RISK_REDUCTION_LOOKBACK_DAYS=120
export EVENT_RISK_CACHE_PATH=.runtime/event_risk.json
export ENABLE_RISK_BLACKLIST=1
export RISK_BLACKLIST_PATH=.runtime/risk_blacklist.json
export RISK_BLACKLIST_CSV_PATH=.runtime/risk_blacklist.csv
export RISK_BLACKLIST_HARD_FILTER=1
export ENABLE_FUNDAMENTALS=0
export FUNDAMENTAL_CACHE_PATH=.runtime/fundamentals.json
export FACTOR_IC_PATH=.runtime/factor_ic.json
export ENABLE_FACTOR_IC_WEIGHTING=0
export FACTOR_IC_MIN_SAMPLES=30
export FACTOR_IC_WEIGHT_BAND=0.3
export CALIBRATE_WALK_FORWARD_FOLDS=4
export CALIBRATE_MIN_COVERAGE=0.5
export EXIT_STOP_LOSS_PCT=5.0
export EXIT_TAKE_PROFIT_PCT=8.0
export EXIT_TRAILING_STOP_PCT=4.0
```

## 接口

- `GET /api/recommendations?top_n=30&market=all`
- `GET /api/sentiment/<code>?name=<股票名>`
- `GET /api/backtest?codes=600000,000001&top_k=10&holding_days=3&mode=rolling`
- `GET /api/backtest?codes=600000,000001&top_k=10&holding_days=3&mode=snapshot`
- `GET /api/strategy-validation/auto-update-status`
- `POST /api/strategy-validation/snapshot?strategy=tomorrow_picks&market=all`
- `POST /api/strategy-validation/prefetch-history?strategy=tomorrow_picks&date=2024-01-01&days=180&update=1`（兼容/内部接口，Web 不再提供按钮）
- `GET /api/reversal-picks?top_n=30&market=all`
- `GET /api/smallcap-value-picks?top_n=30&market=all`
- `GET /api/breakout-picks?top_n=30&market=all`
- `POST /api/strategy-validation/backfill-samples?strategy=tomorrow_picks&days=260&replay_days=20&top_n=36`
- `GET /api/portfolio?strategy=tomorrow_picks`
- `GET /api/portfolio/performance?strategy=tomorrow_picks&days=120`
- `GET /api/paper-trades?strategy=tomorrow_picks&limit=200`
- `GET /api/health`

`market` 可选值：`all`、`main`、`chinext`、`star`。

`/api/recommendations` 会返回：

- `recommendations.short_term`：短期 Top 30。
- `recommendations.long_term`：长期 Top 30。
- `data`：兼容字段，等同于短期 Top 30。
- `meta.market_regime`：当前市场状态、广度、强弱分布和操作建议。
- `recommendations.*[].serenity_profile`：单票质量、风险、置信度、证据和动作建议。
- `recommendations.*[].agent_committee`：TradingAgents 风格委员会结论，包含技术/情绪/基本面代理/新闻环境/牛熊/交易员/风控/组合经理分数和最终动作。
- `meta.strategy_consensus.rows`：多策略重复入选的高共识标的，包含质量、风险、共识分和动作建议。
- `meta.strategy_consensus.serenity_references`：当前 Serenity/chokepoint 与 UZI-Skill 参考来源和对应借鉴点。
- `meta.strategy_consensus.trading_agents_reference`：本轮策略优化参考的 TradingAgents 仓库和借鉴点。

运行时会创建 `.runtime/recommendation_state.json` 保存稳定榜状态。

运行时会创建 `.runtime/history_cache.sqlite3` 保存日线历史数据缓存；策略验证后台自动回填和“回放历史补样本”都会复用这个数据库，避免每次复盘都重新请求远程行情。

运行时也可维护 `.runtime/market_data.sqlite3` 作为更完整的本地日线库，供推荐页历史因子和离线回测校准读取：

```bash
.venv/bin/python -m stock_analyzer.market_data --summary
.venv/bin/python -m stock_analyzer.market_data --download --limit 200 --days 720 --sleep 0.1
```

建议分批下载；`--limit` 会优先处理本地缺失或过期的股票。下载内容包括不复权日线、前复权日线、成交量、成交额和涨跌幅。

如果要把 SQLite 按业务对象拆成多份小文件，直接把 `MARKET_DATA_DB_PATH` 配成目录而不是 `.sqlite3` 文件即可：

```bash
export MARKET_DATA_DB_PATH=.runtime/market_data
.venv/bin/python -m stock_analyzer.market_data --download --limit 200 --days 720 --sleep 0.1
.venv/bin/python -m stock_analyzer.market_data --summary
```

目录模式会把行情元数据/下载状态写入 `.runtime/market_data/market_data_meta.sqlite3`，把日线行情按板块和 5 位代码前缀写入 `market_data_bars_<板块>_<前缀>.sqlite3`，例如 `market_data_bars_main_60000.sqlite3`、`market_data_bars_chinext_30075.sqlite3`。推荐页历史因子、离线校准和 summary 都兼容这种目录模式。旧的单文件 `.runtime/market_data.sqlite3` 仍可继续使用。

第一版 Qlib 风格因子快照表会把本地日线库计算出的 AlphaLite 因子写入 `.runtime/factor_snapshots.sqlite3` 的 `factor_snapshots` 表，主键为 `trade_date + code + factor_set`。当前先保存 3/5/10/20 日收益、均线偏离、量能、突破、波动率和覆盖率，供后续模型训练、DeepSeek 复盘和样本外验证统一读取。策略回溯里的 DeepSeek 复盘会按信号日期和股票代码读取这些快照，并把关键字段压缩进复盘样本，用于生成可验证的降权规则：

```bash
.venv/bin/python -m stock_analyzer.daily_job --factor-snapshot
```

明天预测会把 `MARKET_DATA_DB_PATH` 指向的本地行情库里的历史因子接入盘面门控：当历史 20 日均线宽度低于 45% 时不输出主推买入，只保留备选观察池；45%-55% 时最多保留少量主推；高于 55% 时默认最多保留 5 只主推。页面仍展示完整观察名单，但只有 `tier=primary_watch` 的股票进入真实主样本统计和纸面交易重点。

## 长期黑名单硬过滤

系统默认启用长期黑名单过滤，用于剔除历史财务造假、重大违法、审计无法表示意见、重大内控缺陷、长期严重负面记录等股票。维护文件：

- JSON：`.runtime/risk_blacklist.json`
- CSV：`.runtime/risk_blacklist.csv`
- 模板：[docs/risk_blacklist.example.json](docs/risk_blacklist.example.json)、[docs/risk_blacklist.example.csv](docs/risk_blacklist.example.csv)

建议只录入有权威来源的记录，例如证监会处罚决定、交易所纪律处分、公司公告、审计报告或可信新闻链接。`level=high/critical` 且 `RISK_BLACKLIST_HARD_FILTER=1` 时会从所有推荐候选中硬剔除；`level=medium` 可用于仅提高风险分和解释，不做硬过滤。

## 样本不足处理

策略验证的“样本不足”指验证库里已经有结果、且已经走完该策略主周期的信号少于 30 条，不是单纯缺少K线。只下载历史K线只能更新已保存信号的结果，不能凭空增加过去的推荐样本。

当前策略验证已收敛为只验证“明天预测”：以 14:00 信号价作为尾盘计划入场价，对比次日开盘、最高、最低、收盘，并扣 `VALIDATION_TRADE_COST_PCT` 固定成本和成交额分档滑点。主记分牌、净胜率走势和次日对比只统计真实前瞻样本；回放样本单独显示为参考，不计入主判断。未来交易日不足时，该样本只计入 `outcome_sample_count`，不计入主样本 `sample_count`。其它实时策略仍可展示，但不再进入验证与自动修正闭环。

策略验证还会额外记录 `signal_exit_return` / `exit_reason` / `exit_days` / `exit_date`：按主周期窗口内的止损、止盈、移动止损或持有到期计算退出收益。默认参数是 5% 固定止损、8% 止盈、4% 移动止损，可用 `EXIT_STOP_LOSS_PCT`、`EXIT_TAKE_PROFIT_PCT`、`EXIT_TRAILING_STOP_PCT` 调整。当前已加入可成交性近似：一字涨停/封板买不进的样本会跳过；封跌停止损会顺延到下一交易日开盘。纸面组合默认按持有期摊分每日新开资本，避免每天一组 10/20 日组合被当成不重叠满仓收益。

当前提供两条路径：

1. 真实样本：每天在盘后用 `daily_job` 保存推荐快照，后台会按策略和股票代码分批下载/复用历史 K 线并回填结果；“仅更新当前批次”只用于人工复核。这是最可信的前瞻记录。
2. 离线补样本：点击“回放历史补样本”，系统会下载/复用日线历史，用当前量价规则模拟过去若干个交易日的信号，写入 `*_replay_v1` 版本并立即计算结果。该结果用于快速判断规则是否值得继续跟踪，不等同于真实历史曾经推荐过。

默认回放参数为近 260 日历史、回放 20 个历史交易日、每个交易日保留 Top 36。回放样本仍归入原策略名称统计；同时明细里的 `strategy_version` 会标记为 `tomorrow_picks_replay_v1`，便于和真实保存样本区分。真实前瞻样本优先，回放只用于冷启动粗筛，页面会标记为“回放参考”。

策略共识已加入统一健康状态：真实样本不足为 `pending/probation` 并降权；真实样本数达到 `STRATEGY_DECAY_MIN_REAL_SAMPLES` 后，如果主周期净胜率低于 `STRATEGY_RETIRE_WINRATE` 或主周期净收益为负，该策略进入 `retired`，本轮共识权重置零。

后台自动回填默认参数：

- `VALIDATION_AUTO_UPDATE_ENABLED=1`：启用后台自动任务。
- `VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS=1800`：应用启动后延迟约 30 分钟后首轮，避免阻塞首屏。
- `VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=1800`：每 30 分钟执行一轮。
- `VALIDATION_AUTO_UPDATE_BATCH_SIZE=40`：每批最多处理 40 只股票。
- `VALIDATION_AUTO_UPDATE_MAX_CODES_PER_RUN=160`：每个策略单轮最多处理 160 只股票。
- `VALIDATION_AUTO_UPDATE_HISTORY_DAYS=220`：每只股票预取最近 220 日 K 线。
- `VALIDATION_AUTO_UPDATE_STRATEGIES=`：默认覆盖全部验证策略；可填逗号分隔策略名限制范围。
- `VALIDATION_AUTO_SNAPSHOT_ENABLED=1`：启用服务内置的每日自动保存明天预测。
- `VALIDATION_AUTO_SNAPSHOT_TIME=15:00`：交易日到点自动保存；如果服务在当天 15:00 后才启动，会补保存一次。
- `VALIDATION_AUTO_SNAPSHOT_MARKET=all`：自动保存的市场范围。

## 盘后自动任务

不再通过 Web 按钮下载历史数据。应用服务默认会在交易日 15:00 自动保存当天“明天预测”样本，CLI 可作为外部兜底任务继续保留：

```bash
.venv/bin/python -m stock_analyzer.daily_job --snapshot --strategy all --market all
.venv/bin/python -m stock_analyzer.daily_job --update --strategy all
.venv/bin/python -m stock_analyzer.daily_job --paper-trade --strategy all
.venv/bin/python -m stock_analyzer.daily_job --factor-snapshot
.venv/bin/python -m stock_analyzer.daily_job --factor-ic --strategy all
```

crontab 示例：

```cron
00 15 * * 1-5 cd /home/cp/Public/trader && .venv/bin/python -m stock_analyzer.daily_job --snapshot --strategy all --market all >> .runtime/daily_job.log 2>&1
30 17 * * 1-5 cd /home/cp/Public/trader && .venv/bin/python -m stock_analyzer.daily_job --update --strategy all >> .runtime/daily_job.log 2>&1
30 18 * * 1-5 cd /home/cp/Public/trader && .venv/bin/python -m stock_analyzer.daily_job --update --paper-trade --strategy all >> .runtime/daily_job.log 2>&1
40 18 * * 1-5 cd /home/cp/Public/trader && .venv/bin/python -m stock_analyzer.daily_job --factor-snapshot >> .runtime/daily_job.log 2>&1
45 18 * * 1-5 cd /home/cp/Public/trader && .venv/bin/python -m stock_analyzer.daily_job --factor-ic --strategy all >> .runtime/daily_job.log 2>&1
```

第一条是自动保存的外部兜底；服务内置自动保存和 cron 重复执行时，同一天样本会替换旧样本，不会重复累计。第二条负责 T+1/T+多日结果回填，第三条在回填后同步纸面组合交易和组合净值，第四条刷新因子快照表，第五条刷新因子 IC。应用内后台分批任务仍会继续补 K 线和更新结果。

## 上线后运营流程

代码机制已经覆盖“样本入库 -> 结果回填 -> 纸面组合 -> 因子 IC -> walk-forward 校准”，但是否赚钱取决于真实样本和数据质量。上线后按以下顺序操作：

1. 先让服务持续攒真实样本。应用默认交易日 15:00 自动保存当日明天预测；cron 可作为外部兜底，17:30 回填未来收益，18:30 更新纸面组合，18:45 刷新因子 IC。
2. 每天检查任务日志：

```bash
tail -n 100 .runtime/daily_job.log
```

3. 每天看 Web 两个位置：`明天预测` 看当天 36 支候选；`策略验证` 看真实样本数、次日净胜率、净收益和自动迭代建议。`/api/health` 用于检查事件风险、基本面和 IC 状态。
4. 初期不要马上打开所有 alpha 开关，建议保持：

```bash
export ENABLE_EVENT_RISK=0
export ENABLE_FUNDAMENTALS=0
export ENABLE_FACTOR_IC_WEIGHTING=0
```

5. 如果有 Tushare token，先配置但不立即放开硬过滤：

```bash
export TUSHARE_TOKEN=你的token
```

6. 事件风险先软启用，观察解禁、质押、减持和财报窗口是否误伤：

```bash
export ENABLE_EVENT_RISK=1
export EVENT_RISK_HARD_FILTER=0
```

确认数据质量稳定后，再考虑：

```bash
export EVENT_RISK_HARD_FILTER=1
```

7. 基本面因子后启用。只有确认财务字段覆盖率和更新频率可接受后，再设置：

```bash
export ENABLE_FUNDAMENTALS=1
```

8. IC 加权最后启用。至少等核心策略真实样本达到 30 条，更稳妥是 100 条以上，再考虑：

```bash
export ENABLE_FACTOR_IC_WEIGHTING=1
export FACTOR_IC_MIN_SAMPLES=30
```

9. 权重校准先只跑 dry-run：

```bash
.venv/bin/python -m stock_analyzer.calibrate --calibrate-live-weights all --dry-run
```

只有当 OOS 改善稳定、多数 walk-forward fold 胜出、且纸面组合没有同步恶化时，才去掉 `--dry-run` 写入 `.runtime/weights.json`。

核心原则：先攒真实前瞻样本，再验证数据源，再开启事件风险/基本面/IC 加权。代码就绪不等于策略已有 edge，任何开关都必须经过真实样本验证后再放大使用。

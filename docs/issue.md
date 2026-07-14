# 工程重构 Issue 清单：类/模块级重构以提升性能与高内聚低耦合

> 创建时间: 2026-07-14
> 范围: `stock_analyzer/` 全工程
> 方法: 通读 ~80 个模块、统计体量、识别坏味道，每条给出 `文件:行` 证据、重构方案与预期收益
> 原则: 先低风险高收益（拆 God Module / 提基类 / 去重复），再性能向量化，最后包结构重组

---

## 一、模块体量盘点（God Module 标记 ★）

| 文件 | ~行数 | 职责 | 标记 |
|---|---|---|---|
| `validation/validation_repository.py` | ~3,300 | 10+ 个仓储类 + facade，全部 SQLite CRUD 挤在一个文件 | ★ God Module |
| `services/app_services.py` | ~2,200 | 推荐生成/快照/回测/验证更新/DeepSeek 调度/行情检查 全部用例 | ★ God Module |
| `calibrate.py` | ~1,734 | 目标函数+坐标下降+walk-forward+FDR+交互项+regime+meta+CLI | ★ God Module |
| `providers.py` | ~1,500 | akshare/tushare 适配+实时行情+历史K线+舆情+新闻+安全状态 | ★ God Module |
| `portfolio_baseline.py` | ~1,500 | 日级组合回测+多策略对比+随机基线+指数基线+bootstrap CI | ★ God Module |
| `strategy_validation.py` | ~1,156 | 验证门面类 + outcome 计算（含收盘竞价/14:30后/退市 outcome） | ★ God Class |
| `scoring_core/explanations.py` | ~1,000 | 所有策略的信号解释+理由+serenity profile+committee+交易动作 | ★ God Module |
| `validation_metrics.py` | ~850 | 验证指标计算 | |
| `app_support.py` | ~851 | 候选行构建+行情查找+情感/因子缓存+验证摘要+门控 | |
| `validation_outcomes.py` | ~760 | outcome 服务+执行记录+cost scenario+benchmark | |
| `validation_schema.py` | ~790 | SQLite schema 迁移 | |
| `prediction.py` | ~650 | 预测快照管理 | |
| `fundamentals.py` | ~620 | 基本面因子加载与附加 | |
| `recommendation_runtime_support.py` | ~618 | 推荐运行时协调+DeepSeek 附加+验证门控+payload 元数据 | |
| `paper_trading.py` | ~590 | 纸面交易模拟 | |
| `jobs.py` | ~560 | 后台作业 | |
| `scoring_core/scoring_math.py` | ~515 | percentile+combine+regime+factor IC | |
| `portfolio.py` | ~490 | 组合权重优化+gross exposure+cap 投影+market impact 过滤 | |
| `point_in_time.py` | ~491 | PIT 快照构建（逐行遍历全量行情） | |
| `execution_policy.py` | ~322 | 执行策略构建+成本计算 | |

**`validation_*.py` 家族**: 14 个文件、~8,500 行，构成一个连贯的验证子系统，目前散落在包顶层，应独立为 `validation/` 子包。

---

## 二、顶级重构机会（按影响排序）

### R1【最高】拆分 `validation_repository.py` God Module

- **坏味道**: God Module —— 10+ 个不相关的仓储类塞在单文件
- **证据**: `validation_repository.py` 公开 `__all__` 暴露 `ValidationRepository` + `SignalRepository`/`CandidateSnapshotRepository`/`OutcomeRepository`/`ExecutionRepository`/`PortfolioRepository`/`ExperimentRepository`/`TuningRepository`/`MarketGateRepository`/`ResearchRepository`/`OOSReportRepository`/`PredictionRepository` 共 11 个类
- **重构方案**:
  ```
  validation/repositories/
    ├─ __init__.py            # re-export
    ├─ base.py                # RepositoryBase(共享连接/事务/序列化)
    ├─ signal_repo.py
    ├─ candidate_snapshot_repo.py
    ├─ outcome_repo.py
    ├─ execution_repo.py
    ├─ portfolio_repo.py
    ├─ experiment_repo.py
    ├─ tuning_repo.py
    ├─ market_gate_repo.py
    ├─ research_repo.py
    ├─ oos_report_repo.py
    └─ prediction_repo.py
  validation/facade.py        # ValidationRepository 组合各仓储，保留向后兼容
  ```
- **收益**: 高内聚（每仓储只管自己的表）；低耦合（消费者只导入需要的仓储）；可独立单测；并行开发不冲突

### R2【最高】拆分 `services/app_services.py` God Module

- **坏味道**: God Module + 混合抽象层级（编排、I/O、业务逻辑、序列化混在一起）
- **证据**: `services/app_services.py` 顶部导入 30+ 模块；`AppServices` 类聚合推荐生成、快照 worker、回测触发、验证自动更新、OOS 报告、行情健康检查等互不相关的用例
- **重构方案**: 按用例切分
  ```
  services/
    ├─ recommendation_service.py   # 推荐生成编排
    ├─ validation_service.py       # 验证自动更新/门控
    ├─ backtest_service.py         # 回测触发
    ├─ snapshot_service.py         # 快照 worker
    ├─ oos_service.py              # OOS 报告
    └─ app_services.py             # 退化为薄 facade，组合上述服务
  ```
- **收益**: 单一职责；每服务可独立测试与替换；大幅降低单文件 import 耦合

### R3【高】拆分 `strategy_validation.py` God Class 与超长函数

- **坏味道**: God Class + 超长函数（混合抽象层级）
- **证据**:
  - `StrategyValidationStore`（约 70-379 行）: facade，30+ 委托方法
  - `_compute_outcome`（约 887-1121 行）: **235 行单函数**，混合行情获取 + 退市检测 + 涨停判断 + 收益计算 + 序列化
  - `_compute_close_auction_outcome`（约 539-762 行）: 223 行单函数
- **重构方案**:
  - 把 outcome 计算抽到 `validation/outcome_calculator.py`，按"收盘竞价 outcome / 14:30 后 outcome / 退市 outcome / 观察池 outcome"拆 4 个独立类，共享 `OutcomeContext` 值对象
  - `StrategyValidationStore` 退化为纯门面，委托 `OutcomeCalculator` + 各仓储
  - 每个计算函数压到 < 50 行，提取 `_resolve_security_state`/`_resolve_entry_price`/`_apply_exit_policy` 等私有步骤
- **收益**: 可读性、可测试性大幅提升；退市/涨停等分支可独立覆盖测试

### R4【高】`validation_*.py` 家族独立为 `validation/` 子包

- **坏味道**: 包顶层散落 14 个同前缀文件，缺乏边界
- **证据**: `validation_audit`/`validation_backup`/`validation_benchmarks`/`validation_cache`/`validation_metrics`/`validation_outcomes`/`validation_policy`/`validation_replay`/`validation_repository`/`validation_runtime_support`/`validation_schema`/`validation_serialization`/`validation_stance`/`validation_statistics` 共 ~8,500 行
- **重构方案**:
  ```
  validation/
    ├─ repositories/   # R1
    ├─ outcomes/       # R3
    ├─ metrics.py
    ├─ schema.py
    ├─ audit.py
    ├─ cache.py
    ├─ policy.py
    ├─ replay.py
    ├─ benchmarks.py
    ├─ statistics.py
    ├─ stance.py
    ├─ serialization.py
    └─ backup.py
  ```
  顶层保留 `validation/__init__.py` re-export 保持向后兼容
- **收益**: 清晰子系统边界；新人定位成本下降；避免前缀命名污染包顶层

### R5【高】`scoring_core` 三策略提取 `BaseScorer` 基类

- **坏味道**: 重复代码 —— 三策略共享构造器与样板却无基类
- **证据**:
  - `strategies/tomorrow.py:15` `TomorrowScorer`、`strategies/today.py:15` `TodayScorer`、`strategies/swing_2_5d.py:14` `SwingScorer` **构造器签名完全相同**（`feature_builder`/`risk_policy`/`ranking_policy`/`explanation_builder`/`scoring_context`）
  - 三者都复制 `_ctx`/`_ranking_gate_score`/`_build_candidate_row`/`_select_display_rows` 样板
  - 评分侧 `scoring_core/tomorrow_score.py:90` `_tomorrow_component_scores` 与 `scoring_core/today_score.py:16` `_score_row` 共享"组件分→`_combine_details`→payload→`_build_reasons`"同构流程
- **重构方案**:
  ```python
  class BaseScorer(ABC):
      def __init__(self, feature_builder, risk_policy, ranking_policy,
                   explanation_builder, scoring_context): ...
      def _ctx(self, name, default): ...
      def _ranking_gate_score(self, row): ...
      @abstractmethod
      def _build_candidate_row(self, row, context, market_regime, **kw): ...
      @abstractmethod
      def strategy_name(self) -> str: ...
      # 共享 _select_display_rows / _select_backup_rows 模板方法
  ```
  `TomorrowScorer/TodayScorer/SwingScorer` 仅覆写差异部分
- **收益**: 消除 ~30% 重复；新增策略只需实现差异点；评分流程一致性由基类保证

### R6【高】统一执行成本模型（消除双轨）

- **坏味道**: 重复逻辑 —— 两套成本计算并存
- **证据**:
  - `execution_policy.py:257` `execution_cost_components()` 是规范实现（fee+liquidity+tail+impact）
  - `backtest.py:9` `from .strategy_validation import _execution_cost_pct` —— `strategy_validation` 另有一套 `_execution_cost_pct`，`backtest.py:281` `_backtest_trade_cost_pct` 走这条
  - `portfolio.py:5` 又 `from .strategy_validation import market_impact_cost_pct`，与 `execution_policy.market_impact_cost_pct` 重复
- **重构方案**: 以 `execution_policy.py` 为唯一真相源；`strategy_validation`/`backtest`/`portfolio` 全部委托 `execution_cost_components`；删除 `strategy_validation._execution_cost_pct` 与重复的 `market_impact_cost_pct`
- **收益**: 消除成本口径不一致风险（回测与实盘成本一致）；单一改动点

### R7【高】拆分 `calibrate.py`（~1,734 行）

- **坏味道**: God Module 混合校准/特征工程/统计/CLI
- **证据**: `calibrate.py:93` `_objective`、坐标下降 `_fit_weights`、walk-forward、FDR、`_fit_interaction_terms`、`_fit_regime_specific_weights`、meta-labeling 训练、CLI `argparse` 全在一文件
- **重构方案**:
  ```
  calibration/
    ├─ objective.py        # _objective, _sortino, _time_decay_multiplier
    ├─ coordinate_descent.py
    ├─ ridge.py            # 新增 Ridge 校准
    ├─ walk_forward.py
    ├─ interaction_terms.py
    ├─ regime_weights.py
    ├─ fdr.py              # FDR 门控
    ├─ meta_calibration.py # meta-labeling 训练集成
    └─ cli.py              # argparse 入口
  ```
- **收益**: 每算法独立可测；新校准算法（Ridge/LightGBM）按文件增量加入

### R8【中】拆分 `providers.py`（~1,500 行）

- **坏味道**: God Module —— 多数据源适配 + 多类数据（行情/历史/舆情/新闻/安全）混合
- **证据**: `providers.py` 同时承载 akshare/tushare 适配、实时行情、历史 K 线、舆情、新闻、安全状态查询
- **重构方案**:
  ```
  providers/
    ├─ base.py             # MarketDataProvider 抽象接口
    ├─ akshare_adapter.py
    ├─ tushare_adapter.py
    ├─ realtime_quotes.py
    ├─ history_bars.py
    ├─ sentiment.py
    ├─ news.py
    └─ security_state.py
  ```
- **收益**: 数据源可独立替换/灰度；按数据类型隔离故障；接口契约清晰

### R9【中】`config` 命名空间分区

- **坏味道**: 全局 god-bag —— 所有配置项注入同一 `config` 模块 namespace
- **证据**: `config.py:45-46` 将 `runtime.json` 的 settings 全部 `globals()[_name]=...` 注入；全工程通过 `getattr(config, "X", default)` 耦合（几十处），无分区
- **重构方案**: 不改加载机制，但在 `runtime.json` 内按域分组（`scoring`/`validation`/`portfolio`/`execution`/`calibration`），并提供 `config.scoring`/`config.validation` 等子命名空间视图（`MappingProxyType`）；新增 `config_registry.py` 做分组访问与默认值校验
- **收益**: 配置可按域审阅；减少跨域误改；为"按策略覆盖配置"打基础
- **注意**: 此项影响面广，建议最后做并保留 `getattr(config, ...)` 向后兼容

### R10【中】`scoring_core/explanations.py`（~1,000 行）拆分

- **坏味道**: God Module —— 信号解释/理由/serenity profile/committee/交易动作混在一起
- **证据**: `explanations.py` 同时实现 `_build_tomorrow_reasons`/`_build_reasons`/`_attach_signal_explanation`/`_with_regime_reason`/serenity profile/agent committee/交易动作
- **重构方案**: 拆 `explanations/reasons.py`/`explanations/profile.py`/`explanations/committee.py`/`explanations/trade_action.py`
- **收益**: 解释层职责清晰；profile/committee 可独立演化

---

## 三、性能热点专项

### P1【高】`point_in_time.build_candidate_snapshot_rows` 逐行循环

- **证据**: `point_in_time.py:182` `for position, (_, base_row) in enumerate(base.iterrows())` —— 对全量行情逐行 Python 循环，每行再做多轮 dict 构建、缺失检测、时间戳校验
- **影响**: 快照构建在每次推荐刷新时遍历数千行，是热路径
- **方案**: 向量化可向量部分（缺失检测、时间戳比较用 DataFrame 向量操作）；仅对真正逐行依赖的 `point_in_time_violations` 保留循环；预计算 `candidate_lookup`/`selected_lookup` 为 dict 已有，可进一步用 `code` 索引对齐
- **收益**: 快照构建耗时预计下降 40-70%

### P2【高】`backtest.run_rolling_alphalite_backtest` 嵌套逐码逐日循环

- **证据**: `backtest.py:145` 外层按 signal_point 循环，内层 `for code, history in prepared.items()` 逐码 `compute_alphalite_for_stock`（`factors.py:50` 每次重算均线/收益率）
- **影响**: 滚动回测 O(日期 × 股票数)，因子重复计算
- **方案**: 因子增量计算（仅滑动窗口新增/移除的一根）；或预计算全序列因子再按索引切片；`compute_alphalite_for_stock` 内 `close.tail(5).mean()` 等可缓存滚动窗口
- **收益**: 回测耗时预计下降 50%+

### P3【中】`portfolio._project_weights` 32 轮迭代投影

- **证据**: `portfolio.py:384` `for _ in range(32)` 反复 `_enforce_caps`（内层再 8 轮），纯 Python 循环
- **方案**: 用 cvxpy/scipy 一次性求解带上下界+分组约束的 QP 投影；或至少向量化 cap 投影（按 group 向量缩放）
- **收益**: 组合分仓耗时下降，且更稳定收敛

### P4【中】`candidate_filters` 逐行 `apply`

- **证据**: `candidate_filters.py:119` `df.apply(_is_buyable_gain, axis=1)` 逐行判断；`_candidate_base_frame` 对多列逐列 `map(coerce_number)`
- **方案**: `pct_chg <= MAX_BUYABLE_GAIN_MAIN` 等改为向量化布尔掩码；`coerce_number` 批量化用 `pd.to_numeric(..., errors="coerce")`（部分已有）
- **收益**: 候选过滤在大盘全量行情下提速

### P5【中】`_factor_ic_payload` 文件 mtime 读取

- **证据**: `scoring_math.py:450` 每次评分调用 `os.path.getmtime` 判断缓存是否失效；评分在推荐刷新热路径
- **方案**: 进程内缓存 + 定时刷新（如 60s）而非每次 stat；或订阅文件变更
- **收益**: 减少系统调用，单次评分省去 stat 开销

### P6【低】`simulate_exit` 逐 K 线循环

- **证据**: `risk_rules.py:105` `for idx, row in window.iterrows()` 逐 bar 模拟退出
- **方案**: 该循环天然逐 bar 依赖（移动止损需前高），向量化收益有限；但可对 stop_loss/take_profit 用 `np.where` 一次性找首个触发点，仅 trailing 需循环
- **收益**: 回测批量场景中等提速

---

## 四、重复代码证据汇总

| 重复项 | 位置 A | 位置 B | 方案 |
|---|---|---|---|
| 执行成本计算 | `execution_policy.py:257` | `strategy_validation._execution_cost_pct`（被 `backtest.py:9,281` 引用） | 统一委托 execution_policy（R6） |
| market_impact_cost_pct | `execution_policy.py:233` | `strategy_validation`（被 `portfolio.py:5` 引用） | 统一委托（R6） |
| 三策略 Scorer 构造器/样板 | `strategies/tomorrow.py:18` | `strategies/today.py`/`swing_2_5d.py` 同构造器 | 提取 BaseScorer（R5） |
| 评分组件→combine→payload→reasons 流程 | `scoring_core/tomorrow_score.py:90` | `scoring_core/today_score.py:16` 同构 | 流程上提到 BaseScorer 模板方法（R5） |
| 三策略 risk_penalty_parts | `scoring_core/risk.py:81` tomorrow / `:196` swing | short_term 在 `today_score.py:93` 内联 | 统一到 `risk.py`（补 `_short_risk_penalty_parts`） |
| 权重加载（读 weights.json） | `scoring_core/weights.py:144` | `backtest.py:31` `_load_alphalite_weights` | 统一到 `weights.py` 一个加载器 |
| 退市/涨停判断 | `strategy_validation.py` 多处 | `risk_rules.py:27` `_is_sealed_limit_down` | 抽 `security_status.py` 共享 |

---

## 五、推荐目标包结构

```
stock_analyzer/
├─ config/                     # 配置按域分组视图（R9）
│   ├─ __init__.py             # 兼容旧 getattr 访问
│   └─ registry.py
├─ scoring_core/               # 已较好，补 BaseScorer（R5）
│   ├─ strategies/
│   │   ├─ base.py             # BaseScorer
│   │   ├─ tomorrow.py
│   │   ├─ today.py
│   │   └─ swing.py
│   └─ ... (现有)
├─ strategies/                 # 退化为薄策略对象，继承 BaseScorer
├─ validation/                 # R1+R3+R4 子包
│   ├─ repositories/
│   ├─ outcomes/
│   ├─ metrics.py
│   ├─ schema.py
│   ├─ audit.py
│   └─ ...
├─ calibration/                # R7
├─ providers/                  # R8
├─ execution/                  # 统一成本/退出（R6）
│   ├─ policy.py
│   ├─ cost.py
│   └─ exit.py
├─ services/                   # R2 按用例拆分
└─ portfolio/                  # 可选：把 portfolio.py + portfolio_baseline.py 合并子包
```

---

## 六、优先级矩阵与实施顺序

```
                  高收益
                    │
   R1 validation_repository   R2 app_services
   R3 strategy_validation     R5 BaseScorer
   R6 统一成本模型            R7 calibrate 拆分
                    │
  低风险 ───────────┼─────────── 高风险
                    │
   R4 validation 子包   R8 providers 拆分
   P1 PIT 向量化        P2 回测向量化
                    │
   R10 explanations    R9 config 分区(影响面大)
                    │
                  低收益(相对)
```

**推荐分批实施（每批可独立合并、不破坏对外 API）**:

1. **第 1 批（低风险去重，1-2 周）**: R5 BaseScorer + R6 统一成本模型 + R4 validation 子包（仅移动文件+re-export）
2. **第 2 批（God Module 拆分，2-4 周）**: R1 validation_repository + R3 strategy_validation + R2 app_services
3. **第 3 批（性能向量化，1-2 周）**: P1 PIT 向量化 + P2 回测因子增量 + P3 组合投影
4. **第 4 批（收尾，2-3 周）**: R7 calibrate 拆分 + R8 providers 拆分 + R10 explanations 拆分 + R9 config 分区

---

## 七、实施约束与回归保护

- **对外 API 兼容**: 所有重构通过 `__init__.py` re-export 保持旧导入路径可用（`from stock_analyzer.strategy_validation import StrategyValidationStore` 不变）
- **回归基线**: 每批前跑 `tests/`（136 文件）+ `pytest.ini` 全量；重构后指标必须持平（评分结果 byte-level 不变、回测 metrics 不变）
- **生产冻结开关**: `config.PRODUCTION_FREEZE_ENABLED` 下权重/阈值来自 `production_baseline`，重构不得改变冻结逻辑路径
- **灰度**: 重构类先与旧类并存（`BaseScorer` 与旧 Scorer 并行），shadow 对比一轮再切换

---

## 八、核心判断

当前工程在**评分核心（scoring_core）已具备良好内聚**，但**验证子系统（validation_*）与应用服务层（app_services）存在严重 God Module**，且**三策略重复、成本模型双轨**造成维护与一致性风险。性能上**PIT 快照与滚动回测的逐行/逐码循环**是主要瓶颈。

按"先去重提基类（低风险）→ 拆 God Module（中风险）→ 性能向量化 → 包结构重组"的顺序推进，可在不改变业务行为的前提下显著提升内聚性、降低耦合，并降低热路径耗时 40-70%。

---

## 十一、与 issue.md 已有项对比 + 新增类级重构机会（2026-07-14 复审）

> 本轮复审聚焦 **issue.md 一～八章未覆盖** 的模块（应用组合根、deepseek、routes、7 个策略模块、退出策略、因子注册、recommendation 协调层、scoring_core 内部），并以 `plan.md`（收益提升计划）为对照，识别**结构性重构与策略收益之间的协同/前置关系**。每条标注：与 issue.md 关系（已有/新增/修正）、与 plan.md 关系（赋能前置/冲突/无关）。

### 11.1 与 issue.md 已有项的关系矩阵

| issue.md 项 | 本轮确认 | 与 plan.md 收益项的关联 |
|---|---|---|
| R5 BaseScorer | ✅ 确认，且是 plan.md 10.2「接线因子」的前置（扩展 `STRATEGY_COMBINERS` 在基类模板内更安全） | 赋能 plan.md 10.2 |
| R6 统一成本模型 | ✅ 确认，且是 plan.md 9.2.2「诚实化开关」(`ENABLE_MARKET_IMPACT`/`ENABLE_TAIL_AUCTION_SLIPPAGE`) 的**落点**——成本模型不统一则开关口径不一致 | **前置** plan.md 步骤0 |
| R9 config 分区 | ✅ 确认，直接修复 plan.md 9.2.6（开关写成字符串 `"1"` 实为布尔） | 前置 plan.md 9.2.6 |
| P1/P2/P3 性能热点 | ✅ 确认，与 plan.md 无冲突（纯提速，不改变业务行为） | 无关 |
| R1/R2/R3/R4/R7/R8/R10 | ✅ 确认（God Module 拆分），本轮无新证据推翻 | 无关（纯结构） |

**关键判断**：issue.md 中 **R6（统一成本）与 R9（config 分区）应上提为 plan.md 步骤0 的同批前置**——它们既是结构重构，又是开启收益模块的正确性基础，不做则 plan.md 的验证基线不可信。

### 11.2 新增类级重构机会（R11–R18）

#### R11【高】`app_support.py` 是超大 God Module（混合抽象层）
- **证据**：`app_support.py:118-276` 因子/情感/历史附加；`350-392` `attach_validation_summary`+`attach_score_calibration`+`attach_meta_labeling`；`443-503` 重复校准/元标签附加；`550-751` `strategy_validation_gate_decision`+`dynamic_position_scaling`；`839` `market_news`。跨越因子层、验证层、门控层、仓位层。
- **重构**：拆 `factor_attachment.py` / `validation_attachment.py` / `validation_gate.py`，每模块只负责一个生命周期阶段；`attach_validation_summary` 与 `attach_score_calibration`/`attach_meta_labeling` 收敛为统一 `attach_validation_models(rows, store, strategy, days)`。
- **与 plan.md 关系**：**赋能 plan.md 阶段1**。Meta-Labeling/预期收益/校准的「接线点」就在 `app_support` 的 `attach_*` 函数里；模块混杂=开启模块时改动面大、易漏。先拆再开模块，风险更低。

#### R12【高】验证门控/校准附加在 `app_support` 与 `recommendation_runtime_support` 双重实现（重复）
- **证据**：`app_support.py:443-503` 的 `attach_score_calibration`/`attach_meta_labeling` 与 `recommendation_runtime_support.py:77` `finalize_deepseek_meta`、`105-135` `_persisted_feature_meta` 都围绕「附加模型产物 + 写 meta」。
- **重构**：统一到 R11 的 `validation_attachment.py`，两处委托同一实现。
- **与 plan.md 关系**：plan.md 阶段1 开启模块需改这些附加逻辑；重复=两处都要改→易漏改。与 R11 同批。

#### R13【最高战略价值】7 策略模块缺统一 `BaseAlphaModule` 接口（确认 H1）
- **证据**（无公共 ABC，生命周期各自为政）：
  - `meta_labeling.py:14` `train_meta_label_model` / `:58` `predict_meta_confidence` / `:112` `apply_meta_labeling` —— 纯函数，模型是 `dict`
  - `ensemble.py:23` `ensemble_score` / `:40` `attach_ensemble_score` —— 硬编码 `result["enabled"]=False; result["mode"]="shadow_only"`
  - `expected_return_model.py` —— KNN 式 artifact 读写/晋升门控（独立一套）
  - `probability_calibration.py` —— 唯一有 `ScoreCalibrator` 类
  - `event_alpha.py` —— 纯函数打分
- **重构**：定义 `AlphaModule(ABC)`：
  ```python
  class AlphaModule(ABC):
      name: str
      enabled_switch: str                      # 对应 config 布尔开关
      @abstractmethod
      def train(self, samples) -> dict: ...     # 返回 artifact
      @abstractmethod
      def attach(self, rows, artifact, enforce: bool) -> None: ...
      @abstractmethod
      def evaluate(self, store, days) -> "OOSReport": ...
      def degrade(self) -> None: ...            # 失效自动降级
      @property
      def is_fitted(self) -> bool: ...
  ```
  `MetaLabelingModule`/`EnsembleModule`/`ExpectedReturnModule`/`CalibrationModule`/`EventAlphaModule` 各自实现。
- **与 plan.md 关系**：**直接实现 plan.md 2.3「没有统一集成框架来自动选择/对比/降级模块」**。这是把 plan.md 零散的「逐个开模块」升级为「可编排框架」的关键——类级重构本身即产出收益杠杆，优先级应高于纯结构拆分。

#### R14【最高战略价值】退出策略是 flat dict 而非可配置对象（确认 H2）
- **证据**：`risk_rules.py:9` `default_exit_policy(holding_days)` 返回写死常量 `{stop_loss_pct:5.0, take_profit_pct:8.0, trailing_stop_pct:4.0}`；`simulate_exit(policy: Dict[str,object])` 接受 dict。
- **重构**：`ExitPolicy(ABC)` + `TomorrowExitPolicy` / `SwingExitPolicy` / `RegimeExitPolicy(Delegate)`；持有**已校准**参数，提供 `simulate_exit(window, entry, policy: ExitPolicy)`。校准结果 = 构造不同 policy 实例，而非改全局常量。
- **与 plan.md 关系**：**直接支撑 plan.md 9.3（最高 ROI 杠杆）**。当前 plan.md 9.3 只能改全局 `EXIT_*_PCT` 常量（影响所有策略/市况），类化后可 **per-strategy / per-regime** 实例化——这是 plan.md 退出校准能落地的工程前置。优先级最高。

#### R15【高】因子硬编码 + `ENABLE_ENHANCED_FACTORS` 散点门控（确认 H4）
- **证据**：`factors.py:102` `ENABLE_ENHANCED_FACTORS` 分支内联计算 `close_vs_vwap`/wick 比率等；`scoring_core` 任何 score 函数都不引用增强因子（plan.md 10.2 已指出）；`normalization.py:48-50` 别名表含 `main_net_flow_1d`/`order_imbalance` 但无 score 引用。
- **重构**：`FactorRegistry` 声明式注册（name, compute, pit_safe, availability, default_weight），接线因子 = 注册 + 在 `STRATEGY_COMBINERS` 引用，而非改多处代码/散点 `if ENABLE_*`。
- **与 plan.md 关系**：**直接支撑 plan.md 10.2（接线已流转因子）**。把「加一个 term」变成「注册一个因子」，消除散点门控，使 plan.md 10.2 的灰度接线可配置化。

#### R16【中】`ApplicationContainer` 组合根（确认 H3，偏良性）
- **证据**：`app_container.py:358` `__init__` 直接 `new` 约 20 个协作者（provider、`8×TimedCache`、`2×PayloadCache`、`TopKDropoutTracker`、`StrategyValidationStore`、`ValidationMetricsCache`、`AsyncSnapshotWriter`、`CandidatePipeline`、`RealtimeMarketScheduler`、`TomorrowIterationService`）；`397-420` `cached_*`/`cache_health` 委托方法。
- **重构**：提取 `ContainerBuilder` / 协作者通过工厂注入（至少 caches 可注入）；委托方法下沉到各自协作者。
- **收益**：可测试性（注入 mock）、低耦合。
- **与 plan.md 关系**：无关（基础设施）；但 R11 动 `app_support` 会触达 container，建议 R11/R12/R16 同批。

#### R17【正向发现】`deepseek/` 已是良好内聚范例（非问题）
- **证据**：`deepseek/` 14 文件职责清晰（`cache`/`http_client`/`feature_service`/`meta_training`/`meta_model`/`payload_builder`/`feature_schema`/`feature_dependencies`/`news_context`/`event_score`/`evidence_validation`/`research_policy`/`runtime_features`）。
- **备注**：`feature_schema.py`(618) 仍可拆（契约常量 + 响应校验 + 特征归一化混一）；整体作为「应推广到其他模块」的范式。
- **与 plan.md 关系**：无关。

#### R18【中】`scoring_core/scoring_math.py`(515) 与 `recommendation_runtime_support.py`(618) 可再拆
- **证据**：`scoring_math.py` 混合 percentile/combine/regime/factor_ic 多基元；`recommendation_runtime_support.py` 与 `recommendation_policy.py` 及 `app_support`（R12）存在编排重叠。
- **重构**：`scoring_math.py` 拆 `percentile.py`/`combine.py`/`factor_ic.py`/`regime_weight.py`；`recommendation_runtime_support` 与 `app_support` 的重叠按 R12 收敛。
- **与 plan.md 关系**：无关（纯结构），但 `scoring_math` 拆后便于 plan.md 的 Factor-IC 加权独立演进。

### 11.3 对 issue.md 已有项的修正/细化

- **R5 BaseScorer**：补充——它也是 plan.md 10.2 的前置；在基类模板内扩展 `STRATEGY_COMBINERS` 比在三个 Scorer 各自改更安全。
- **R6 统一成本模型**：上提为 **plan.md 步骤0 同批前置**（成本口径不统一则 9.2.2 诚实化开关无意义）。
- **R9 config 分区**：补充——`ENABLE_*` 开关散落全工程（`getattr(config,...)` 几十处），分区后可为 plan.md 的模块开关提供**类型安全 registry**，并一劳永逸修复 9.2.6 的布尔/字符串混乱。

### 11.4 与 plan.md 策略项的交叉影响矩阵（核心对比）

| plan.md 收益项 | 依赖的 issue.md 重构 | 关系 |
|---|---|---|
| 步骤0 诚实化开关 (9.2.2) | R6 统一成本 + R9 config 分区 | **前置/赋能** |
| 步骤1 退出校准 (9.3) | R14 ExitPolicy 类化 | **前置**（否则只能改全局常量，无法 per-strategy/regime） |
| 步骤1 接线因子 (10.2) | R15 FactorRegistry + R5 BaseScorer | **前置**（否则散点改代码） |
| 阶段1 开 7 模块 (1.1–1.5) | R13 BaseAlphaModule + R11/R12 app_support 拆分 | **前置**（开启模块的接线点在 app_support，重复/混杂增加出错面） |
| 阶段3 集成 (3.1) | R13 BaseAlphaModule | **直接实现**（框架即此重构产物） |
| 阶段2 swing 校准 (2.1) / Ridge (2.2) | R7 calibrate 拆分 | 赋能（calibrate 拆后可独立加 Ridge） |
| 阶段4 ML 排名 (4.1) | R13 BaseAlphaModule | 赋能（新模型按 `AlphaModule` 接入即可） |

### 11.5 融合 issue.md 与 plan.md 的统一优先级

```
第一批(结构即收益前置, 1-2周):  R14 ExitPolicy  + R15 FactorRegistry
                                  + R6 统一成本   + R9 config 分区
        ↓ 解锁 plan.md 步骤0/1 的全部收益杠杆
第二批(模块框架, 2-3周):        R13 BaseAlphaModule
                                  + R11/R12 app_support 拆分 (R16 同批)
        ↓ 解锁 plan.md 阶段1 开模块 + 阶段3 集成
第三批(纯结构 God Module, 2-4周): R1 validation_repository
                                  + R3 strategy_validation + R2 app_services + R7 calibrate
第四批(性能向量化, 1-2周):        P1 PIT + P2 回测 + P3 组合投影
第五批(收尾, 2-3周):             R4 validation 子包 + R8 providers
                                  + R10 explanations + R17/R18 微调
```

**核心判断**：本轮复审发现，**最高价值的类级重构不是拆 God Module，而是 R13（BaseAlphaModule）/ R14（ExitPolicy）/ R15（FactorRegistry）——它们本身就是 plan.md 收益杠杆的工程前置/实现体**。先做的结构重构应优先选「同时解锁收益」的那几项（R14/R15/R6/R9/R13/R11），而非按纯结构风险排序。纯性能热点（P1-P3）与纯结构拆分（R1-R4/R7/R8/R10）可并行或后置，不阻塞收益。

> 全部重构通过 `__init__.py` re-export 保持旧导入路径可用，且以 plan.md 的 OOS 门控（改善>0.05、4 折≥3 正面、FDR q=0.1）作为回归基线。

---

## 十二、第四轮复核：与 issue.md 已有项对比 + 新增发现（2026-07-14 四审）

> 本轮聚焦 issue.md 一～十一章**未深入覆盖**的模块（数据流管线、序列化/序列化、SQLite 连接管理、scoring_core 内部泄露、services 目录结构、测试基础设施、快照编排层），并与 issue.md 已有项做精确交叉对比。

### 12.1 与 issue.md 已有项的精确关系矩阵

| issue.md 已有项 | 本轮确认 | 细化/修正 |
|---|---|---|
| R1 validation_repository 拆分 | ✅ 追加证据：14 个模块共享同一套 SQLite 连接模式（`sqlite3.connect`+`PRAGMA foreign_keys=ON`+`PRAGMA busy_timeout`），但 4 个模块（`daily_data`/`factor_snapshot`/`history_cache`/`market_data`）自己直连，不走 `sqlite_support.py` 统一入口 | 补充到 R1：拆分时应同时统一 SQLite 连接 → `sqlite_support` |
| R3 strategy_validation 超长函数 | ✅ 确认 `_compute_outcome` 235 行；`validation_audit.py` 中 `audit_point_in_time` 也是 **单一函数 394 行**，属于同一坏味道 | 补充 R19（见下） |
| R4 validation 子包 | ✅ 确认，并发现 `validation_backup.py` 与 `validation_replay.py` 有重复的 `_sqlite_backup` 函数 | 补充到 R4 |
| R5 BaseScorer | ✅ 确认三策略 Scorer 无基类；`strategies/__init__.py` 纯 re-export | 无修正 |
| R11 app_support 拆分 | ✅ 确认；`snapshot.py:29` `run_snapshot` 与 `app_support` 共享编排职责 | 补充：`snapshot.py` 应在 R11 同批考虑 |
| R13 BaseAlphaModule | ✅ 确认 | 无修正 |
| P1/P2/P3 性能热点 | ✅ 确认 | 无修正 |

### 12.2 本轮新增发现（R19–R26）

#### R19【高】`validation_audit.py` 的 `audit_point_in_time` 是 394 行单函数 God Function

- **坏味道**：超长函数（混合 SQL 查询 + JSON 反序列化 + 时间戳校验 + 收益复现性验证 + 执行策略版本一致性 + 基准复现性 + 数量守恒校验 + 退市标签校验）
- **证据**：`validation_audit.py:12-393` `def audit_point_in_time` 单一函数 382 行，内含 60 行嵌套 SQL、8 类独立校验。与 `strategy_validation._compute_outcome`（R3，235 行）同属"验证函数的 God Function"坏味道
- **重构**：拆为 `_audit_timestamps` / `_audit_features` / `_audit_execution_policy` / `_audit_costs` / `_audit_returns` / `_audit_quantities` / `_audit_benchmarks` / `_audit_delisting`，各 < 50 行，`audit_point_in_time` 退化为编排器
- **收益**：可读/可测；每类校验可独立单测覆盖；新增校验类型只需加一个子函数
- **与 issue.md 关系**：补充到 R3 同批（验证子系统的超长函数拆分）

#### R20【中】SQLite 连接管理分散 —— 4 个数据模块绕过 `sqlite_support.py` 统一入口

- **坏味道**：重复基础设施 —— `sqlite_support.py:13` 提供 `open_sqlite`（WAL+foreign_keys+busy_timeout 统一初始化），但 4 个模块直连 SQLite 而不走此入口
- **证据**：
  - `daily_data.py:11` `from .sqlite_support import sqlite_transaction`（走了统一入口 ✅）
  - `factor_snapshot.py`、`history_cache.py`、`market_data.py` 各自 `import sqlite3` + `sqlite3.connect()` + 内联 `PRAGMA foreign_keys=ON`——与 `sqlite_support.py` 重复
  - `validation_backup.py:338` 有独立的 `_sqlite_backup`（`sqlite3.connect`+`.backup()`），而 `sqlite_support.py` 未提供备份功能
  - `validation_audit.py:20` 用 `store.repository.connect()` 而非 `open_sqlite`
- **重构**：`sqlite_support.py` 扩展为 `SQLiteConnectionManager` 类，提供 `connect`/`transaction`/`backup` 三合一；所有 14 个模块统一委托
- **收益**：WAL/超时/外键设置一处改动全局生效；便于未来加连接池/只读副本
- **与 issue.md 关系**：补充到 R1/R4 同批（validation 子包统一基础设施）

#### R21【中】`scoring_core/__init__.py` 是扁平 re-export（确认 H5，良性但需补全）

- **证据**：`scoring_core/__init__.py:6-14` `_MODULE_EXPORTS` 列出 8 个子模块名 + `_CLASS_EXPORTS` 列出 4 个类（`FeatureBuilder`/`ExplanationBuilder`/`RankingPolicy`/`RiskPolicy`），用 `__getattr__` 惰性加载。但 `swing_score.py`、`weights.py`、`risk.py`、`theme_scores.py`、`theme_constants.py`、`policies.py`、`horizon.py`、`expected_return.py` 不在导出表中——意味着**这些模块被隐式消费时依赖调用方知道内部路径**（如 `from .scoring_core.swing_score import ...`）
- **重构**：补全导出表（至少加入 `swing_score`/`weights`/`risk`/`theme_scores`/`policies`）；或改为 `__all__` 声明式 + 类型存根
- **收益**：调用方只依赖 `scoring_core` 命名空间，内部重构不破坏调用方
- **与 issue.md 关系**：低优先级收尾（可与 R5 同批）

#### R22【中】`snapshot.py`(440 行) 与 `app_support.py` 共享编排职责

- **坏味道**：职责重叠 —— `snapshot.py:29` `run_snapshot` 编排行情获取→候选构建→评分→DeepSeek 附加→PIT 事件/基本面过滤→信号保存的全流程；`app_support.py` 也有相似的管线编排
- **证据**：`snapshot.py:12-22` 导入 `build_alphalite_factors`/`merge_alphalite`/`attach_fundamental_factors`/`load_fundamentals`/`filter_point_in_time_events`/`filter_point_in_time_fundamentals`/`attach_persisted_deepseek_features`/`attach_event_risk`/`build_market_regime`/`prepare_candidates`——与 `app_support.py` 的导入集高度重叠
- **重构**：`snapshot.py` 与 `app_support`（R11）拆分后的 `factor_attachment.py`/`validation_attachment.py` 统一委托同一组生命周期函数
- **与 issue.md 关系**：R11 同批（app_support 拆分时一并收敛）

#### R23【中】`services/` 目录只有一个 `app_services.py`（确认 H9）

- **证据**：`services/` 目录仅含 `app_services.py`(2200 行 God Module)。而 `tomorrow_iteration.py`(77 行)、`recommendation_runtime_support.py`(618 行)、`realtime_schedule.py`(61 行) 都是服务类但散落在包顶层
- **重构**：`services/` 按 R2 拆分为 `recommendation_service.py`/`validation_service.py`/`backtest_service.py`/`snapshot_service.py`/`oos_service.py`；将 `TomorrowIterationService`/`RecommendationService`/`RealtimeMarketScheduler` 移入
- **收益**：`services/` 成为统一的服务层入口；包顶层减少散落服务类
- **与 issue.md 关系**：补充到 R2（app_services 拆分）

#### R24【中】测试基础设施良好但根级无 `conftest.py`（H10 部分确认）

- **证据**：
  - `tests/scoring/conftest.py:3-18` 已有共享 fixtures（`quotes`/`codes`/`risk_blacklist_files`/`make_history_frame`/`make_validation_history`/`make_fake_provider`/`validation_store`/`patched_app`）+ `pytest_collection_modifyitems` 自动标记
  - `tests/scoring/helpers.py` 提供 `quote_frame`/`history_frame`/`fake_provider`/`make_validation_store` 等工厂
  - 但 `tests/` 根目录**无 `conftest.py`**，42 个测试文件中有 ~22 个在 `tests/scoring/` 外，无法自动继承 scoring 的 fixtures
- **重构**：`tests/conftest.py` 薄封装，import scoring 的 fixtures 或提供根级通用 fixture（如 `tmp_path` 包装）
- **收益**：非 scoring 测试也能用验证 store/行情 fixture
- **与 issue.md 关系**：低优先级收尾

#### R25【正向发现】`runtime_json.py` 原子写入工具已是良好范式

- **证据**：`runtime_json.py:9` `atomic_write_json`（`mkstemp`→写入→`fsync`→`os.replace`→`finally remove temp`）是教科书级实现。但全工程有多处直接 `json.dump` 到文件（非原子），应推广此工具
- **建议**：不做重构，而是推广使用——在 issue.md 中记录为「应推广的正面范式」

#### R26【低】`scoring_core/theme_scores.py`(350 行) 与 `theme_constants.py`(150 行) 混合数据与逻辑

- **证据**：`theme_scores.py:21-40` 内含 `SERENITY_REFERENCES` 数据字典、`CHOKEPOINT_KEYWORDS` 元组等配置数据；`theme_constants.py:8` `CHOKEPOINT_CHAIN` 与 `TECH_THEMES` 也是数据
- **重构**：`theme_data.py`（纯数据）与 `theme_scores.py`（纯计算）分离
- **收益**：配置可独立维护/审计
- **与 issue.md 关系**：低优先级，可与 R9 config 分区同批

### 12.3 新增验证项（H5-H10 结论）

| 假设 | 结论 | 证据 |
|------|------|------|
| H5: scoring_core `__init__.py` 扁平泄露 | **部分成立** | `_MODULE_EXPORTS` 缺 `swing_score`/`weights`/`risk` 等，调用方用内部路径 → R21 |
| H6: validation_*.py 重复 SQLite 连接 | **成立** | 4 个数据模块绕过 `sqlite_support`；`validation_backup.py` 有独立 `_sqlite_backup` → R20 |
| H7: recommendation_snapshot/freeze 重复序列化 | **不成立** | `recommendation_snapshot.py`(97 行) 薄封装 `runtime_json.atomic_write_json`；`recommendation_freeze.py`(18 行) 纯门控检查——各自独立、无重复 |
| H8: backtest.py 重复信号准备 | **不成立** | `backtest.py` 的信号准备已委托 `build_alphalite_factors`/`merge_alphalite`，无内联重复 |
| H9: services/ 目录欠填充 | **成立** | 仅 `app_services.py`；`TomorrowIterationService`/`RecommendationService` 等在包顶层 → R23 |
| H10: 测试无共享 conftest | **部分成立** | `tests/scoring/conftest.py` 已有良好 fixtures，但 `tests/` 根目录无 conftest，22 个非 scoring 测试无法继承 → R24 |

### 12.4 修订后的统一优先级（融合四轮审查）

```
第一批(结构即收益前置, 1-2周):
    R14 ExitPolicy + R15 FactorRegistry + R6 统一成本 + R9 config 分区
    (解锁 plan.md 步骤0/1)

第二批(模块框架+管线收敛, 2-3周):
    R13 BaseAlphaModule + R11/R12/R22 app_support+snapshot 收敛 + R16 同批

第三批(验证子系统 God 拆分, 2-3周):
    R1 validation_repository + R3 strategy_validation + R19 audit 超长函数
    + R20 SQLite 统一 + R4 validation 子包

第四批(服务层重组, 2-3周):
    R2 app_services + R23 services 目录填充

第五批(纯结构 God, 2-4周):
    R7 calibrate + R8 providers + R10 explanations

第六批(性能向量化, 1-2周):
    P1 PIT + P2 回测 + P3 组合投影

第七批(收尾, 1-2周):
    R21 scoring_core 导出补全 + R24 测试 conftest + R26 theme 数据分离 + R25 推广原子写入
```

**核心判断**：四轮审查共发现 26 项类级重构机会。其中 **R14/R15/R13/R6/R9 应最先做**——它们不仅是结构改善，更是 plan.md 收益杠杆的工程前置。验证子系统的超长函数（R19）和 SQLite 连接碎片化（R20）是第二轮才发现的新盲区，应排在第三批与 R1/R3/R4 同批处理。`snapshot.py` 与 `app_support` 的编排重叠（R22）应在 R11 同批收敛，避免拆后又引入新重复。

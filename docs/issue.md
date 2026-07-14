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

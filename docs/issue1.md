# `issue.md` 复核结论与完整修复计划

> 复核时间：2026-07-14
>
> 复核范围：`docs/issue.md`、`stock_analyzer/`、相关测试与本地只读性能样本
>
> 文档性质：工程重构实施计划，不直接修改评分口径、冻结生产基线或历史验证结果
>
> 输出目标：区分真实问题、已部分完成的重构、误判和暂不值得实施的优化，并给出可独立合并、可验证、可回退的修复顺序

---

## 1. 总结论

`issue.md` 对“大文件过多、验证子系统边界不清、热路径存在逐行处理”的总体判断成立，但不能按原顺序直接实施。原扫描有四类偏差：

1. **把已存在的分层当成完全未拆分**：`validation_repository.py` 已有 `_RepositoryBase` 和组合式 `ValidationRepository`；`app_services.py` 已有多个 UseCase 类，只是实现仍集中在 `_AppServiceContext`。
2. **把兼容转发误判成重复算法**：成本规范实现已经在 `execution_policy.py`；`validation_policy.py` 主要是兼容包装，不是第二套独立算法。
3. **低估兼容与行为风险**：直接移动 `validation_*.py` 并新增 `validation/__init__.py`，不能保留 `stock_analyzer.validation_metrics` 等旧模块路径；模块移动还会使大量 `unittest.mock.patch` 路径失效。
4. **遗漏更紧急的问题**：执行成本调用缺少策略上下文时会得到 0；`validation_repository.__all__` 含不存在的符号；百分位函数每次评分都重新排序；`portfolio_baseline.py` 的 1,507 行没有进入正式拆分项。

建议后的优先级是：

```text
行为正确性与兼容基线
    ↓
已证实的热路径优化
    ↓
解除 strategy_validation 反向依赖
    ↓
拆 validation repository / outcome / portfolio baseline
    ↓
把 AppServices 的实现真正移入用例
    ↓
小范围复用 Scorer、拆 explanations
    ↓
拆 calibration / providers
    ↓
最后再做配置分域和剩余包迁移
```

不建议当前引入 `cvxpy`/`scipy` 只为替换最多约 10 个持仓的权重投影，也不建议向量化具有顺序依赖的退出模拟。两项都应在真实 profiler 证明其占比后再做。

---

## 2. 复核基线

### 2.1 当前代码体量

| 文件 | 实际行数 | 复核结论 |
|---|---:|---|
| `validation_repository.py` | 3,044 | 大文件成立；已存在基类和门面，问题是物理聚集、重复旧类及职责混入 |
| `services/app_services.py` | 2,145 | 大文件成立；已有 UseCase 外壳，但核心实现仍在约 1,600 行的 `_AppServiceContext` |
| `calibrate.py` | 1,733 | God Module 成立 |
| `providers.py` | 1,542 | God Module / God Class 成立；复核期间新增的分页修复逻辑不改变该判断 |
| `portfolio_baseline.py` | 1,507 | God Module 成立，原清单遗漏正式修复项 |
| `strategy_validation.py` | 1,155 | 长 outcome 函数成立；`StrategyValidationStore` 已基本是门面 |
| `scoring_core/explanations.py` | 1,079 | 多职责成立 |
| `app_support.py` | 850 | 较大，但当前优先级低于上述核心边界 |
| `validation_schema.py` | 680 | 以顺序迁移为主，单纯按行数拆分价值有限 |

仓库中实际有 **15 个** `validation_*.py` 文件，共 **7,706 行**；若排除 CLI 文件 `validation_audit_cli.py`，则是原文所指的 14 个模块、共 7,540 行，不是约 8,500 行。

### 2.2 当前测试基线

复核时执行：

```bash
scripts/test.sh fast
```

结果通过。当前测试能作为后续重构的行为基线，但尚缺少以下契约：

- `validation_repository.__all__` 完整性；
- 旧模块导入路径矩阵；
- 成本调用必须携带策略或冻结 policy；
- 大盘候选评分与 PIT 快照的性能回归；
- 拆分前后推荐 payload 和 SQLite 行内容的深度等价。

### 2.3 本地只读性能抽样

以下数据只用于确认热点，不能作为跨机器绝对 SLA：

- `.runtime/latest_quotes.json` 有 5,527 行，当前硬过滤后约 3,397 行。
- `build_candidate_snapshot_rows` 对本地快照前 100 行单次约 **6.236 秒**；热点不只是 `iterrows`，还包括每行创建单行 DataFrame、反复解析时间戳和逐特征构造审计字典。
- `percentile_score` 对 3,000 个值逐一计算排名约 **5.579 秒/单因子**，因为每次调用都重新排序同一列表；三策略每行会调用多个百分位，这是原清单遗漏的首要评分热点。
- `_project_weights` 在 10 行输入上执行 1,000 次约 **0.305 秒**，即约 0.305ms/次；目前没有引入 QP 求解器的性能理由。

---

## 3. 对 `issue.md` 各项的逐条判定

### 3.1 架构重构项

| 编号 | 判定 | 复核说明 | 修订后的处理 |
|---|---|---|---|
| R1 repository 拆分 | **确认，但方案需修正** | 已有 `_RepositoryBase` 和薄门面；门面实际组合 9 个仓储。`PortfolioRepository` 与 `ExperimentRepository` 是未被门面使用的旧重复实现 | 先清理导出和重复类，再物理拆文件；同时把非 CRUD 的 prediction outcome/metrics 移到服务层 |
| R2 AppServices 拆分 | **确认，但已经部分完成** | 已有 `RecommendationUseCase`、`ValidationUseCase` 等，但它们只转发到 `_AppServiceContext`；`AppServices` 又重复转发一遍 | 把实现和所需状态真正搬到各 UseCase，Context 只保留依赖、锁和共享缓存 |
| R3 strategy_validation 拆分 | **确认** | `_compute_close_auction_outcome` 约 226 行，`_compute_outcome` 约 237 行；Store 已主要是 facade | 提取纯 outcome calculator 和安全状态/入场/退出步骤；保留顶层兼容别名 |
| R4 validation 子包 | **部分确认，高迁移风险** | 子系统边界确实需要显式化，但一次移动 14/15 个模块收益小、冲突大；仅在新包 `__init__` re-export 不能保留旧模块路径 | 随 R1/R3 增量建立 `validation/`；每个旧顶层模块保留显式 shim，最后再评估是否删除 |
| R5 BaseScorer | **部分确认，原重复量被高估** | 三个构造器和 `_ctx` 相同；候选池冻结排名逻辑重复。Today 没有 `_ranking_gate_score`，`_select_display_rows` 也不是三策略共有 | 只提取稳定公共生命周期，不把差异很大的 display/tier/score 流程强塞进模板基类 |
| R6 成本模型统一 | **核心实现已统一，但存在调用正确性问题** | `validation_policy` 已委托 `execution_policy`；真正问题是多个模块经 `strategy_validation` 反向导入，以及调用时缺失 strategy/policy | 解除反向依赖，并强制成本 API 显式接收策略或冻结 policy；修复 backtest/paper trading 的零成本 |
| R7 calibrate 拆分 | **确认** | 目标函数、walk-forward、FDR、交互项、regime、meta、CLI 混合 | 只做等价拆分；本轮不顺带新增 Ridge/LightGBM |
| R8 providers 拆分 | **确认，但不应把现有门面改成抽象基类** | `MarketDataProvider` 同时做报价编排、历史、新闻、事件、基本面、缓存与状态 | 保留稳定 facade，内部用 Protocol + 组合适配器；兼容 duck-typed 测试 provider |
| R9 config 分区 | **确认但应最后实施** | 416 处 `getattr(config, ...)`，测试大量 `patch.object(config, ...)`；直接分组 JSON 会影响生产指纹 | 先加动态只读 domain view 和 schema 校验，保留所有平铺属性；JSON schema v2 单独迁移 |
| R10 explanations 拆分 | **确认** | 理由、风险画像、委员会、交易/退出动作确实混合 | 利用已有 `ExplanationBuilder` 作为稳定门面，将 `explanations` 转为同名 package 并在 `__init__` 明确 re-export |

### 3.2 性能项

| 编号 | 判定 | 复核说明 | 修订后的处理 |
|---|---|---|---|
| P1 PIT 快照 | **确认，优先级提升为 P0/P1** | 100 行抽样已达到秒级；主要问题不只是 `iterrows` | 一次性规范化整帧、预计算 mask/时间边界/来源字典，行循环只组装不可向量化的嵌套审计对象 |
| P2 rolling backtest | **确认，收益幅度待基准验证** | 每个日期、每只股票重复 copy、rename、数值规范化和滚动统计 | 每只股票预计算因子 panel，回测按日期索引读取；先保证逐日期结果等价 |
| P3 portfolio 32 轮投影 | **暂不实施** | 默认最大持仓很小，本地 10 行约 0.305ms/次 | 只补收敛/约束正确性测试；真实 profiler 证明占比后再决定是否向量化，禁止当前新增重依赖 |
| P4 candidate_filters apply/map | **确认** | `buyable_gain` 可直接按 market 做布尔 mask；数值列可批量规范化 | 提供与 `coerce_number` 等价的批量数值转换，不能直接用会丢失逗号/%语义的裸 `pd.to_numeric` |
| P5 Factor IC stat | **确认但当前为潜在热点** | 开关关闭时不会 stat；开启后每个 component/row 都可能调用。缓存还没有把 path 纳入 key | 缓存 key 使用 `(path, mtime_ns)`，加短间隔 stat 节流或每次评分批次只解析一次 |
| P6 simulate_exit 循环 | **暂不实施** | 持有窗口短，且 trailing、涨跌停延迟和最早退出日有明确顺序依赖 | 保留清晰循环；只在大规模 profiler 证明占比后优化，并必须做逐分支等价测试 |

### 3.3 重复代码项

| 原结论 | 复核结果 |
|---|---|
| 两套执行成本 | 算法已经单源；保留的是 compatibility wrapper。应修调用上下文和依赖方向，而不是再删除一套算法 |
| 两套 market impact | 同上；`validation_policy.market_impact_cost_pct` 是对冻结 policy 的包装，不能直接无条件替换成不带 policy 的底层调用 |
| 三策略完整模板重复 | 只有依赖初始化、context 和冻结候选池等小部分稳定重复；展示门控、降级和 tier 逻辑差异很大 |
| short-term risk penalty 与其他策略重复 | short-term 的 reversal/sentiment/near-limit 规则语义不同；可为内聚移入 `risk.py`，但不算算法重复 |
| 两套 weights.json 加载 | 文件相同但 section 和冻结规则不同。应建立统一 artifact reader，再保留各 section 的 schema/默认值 |
| 退市/涨跌停判断重复 | 买入涨停、退出跌停、证券退市状态是不同概念；可共享价格限制和状态值对象，不能合并成一个模糊函数 |

---

## 4. 原清单遗漏的高优先级问题

### N1【P0 正确性】成本 API 缺少显式策略时返回零成本

当前链路：

```text
backtest._backtest_trade_cost_pct({turnover})
  → strategy_validation._execution_cost_pct
  → validation_policy.execution_cost_pct
  → policy_from_signal(strategy_name="")
  → build_execution_policy("")
  → 非可执行策略的 fee/slippage/impact 均为 0
```

`paper_trading._evaluate_trade` 明明持有 `strategy_name`，也只传了 `turnover`。复核时：

- 无 `strategy_name`：高流动性与低流动性成本都为 `0.0`；
- 传 `strategy_name="tomorrow_picks"` 的低流动性示例成本为 `1.3325%`。

现有部分 legacy 测试只断言“二者相等”，因此把错误行为固化成了绿灯。修复要求：

1. 底层 `execution_cost_components` 继续作为唯一计算源。
2. 新增明确入口 `execution_cost_for_strategy(row, strategy_name, policy=None)`；不得静默接受空策略用于可执行回测。
3. backtest 增加 `strategy_name` 或 `execution_policy` 参数；AlphaLite 默认建议显式使用 `tomorrow_picks` 的研究成本口径，并在输出中记录 `cost_policy_version`。
4. paper trading 优先读取信号冻结的 `execution_policy`，旧记录缺失时才按真实 `strategy_name` 构建。
5. 这是行为修复，不得伪装成纯重构：需要提升相关报告/baseline 版本，旧结果不原地重写。

### N2【P0】`validation_repository.__all__` 含不存在的类

`__all__` 包含 `MarketGateRepository`，文件中没有该定义。显式 `import *` 会抛 `AttributeError`。应立即：

- 从 `__all__` 删除无效符号；
- 添加 `test_module_all_exports_exist`；
- 检查所有模块的 `__all__`，避免拆包后再次出现同类问题。

### N3【P1】repository 内有未使用的重复旧类

- `PortfolioRepository` 的 tuning CRUD 与后面的 `TuningRepository` 重叠；
- `ExperimentRepository` 的 fold prediction CRUD 与后面的 `ResearchRepository` 重叠；
- `ValidationRepository` 没有实例化前两者。

拆文件前应先做方法/SQL 等价测试，将内部使用统一到 `TuningRepository` 和 `ResearchRepository`。为兼容潜在外部显式导入，可保留一轮 deprecated alias；不要把重复实现分别搬入新文件。

### N4【P1 性能】百分位评分重复排序

`normalization.percentile_score` 每次调用都执行：

```python
sorted([v for v in values if ...])
```

同一个 context 在每只股票、每个组件上被反复排序，复杂度远高于必要值。修复建议：

1. `FeatureBuilder.score_context` 生成不可变的 `SortedNumericValues`，每列只清洗和排序一次。
2. 使用 `bisect_right` 保持当前“`<= value`”和重复值语义。
3. `percentile_score` 继续兼容普通 iterable，只有预排序类型走快速路径。
4. 对空值、全相等、正负无穷、重复值和 `higher_is_better=False` 做精确等价测试。

### N5【高】`portfolio_baseline.py` 是遗漏的 God Module

`DailyPortfolioBaselineService` 同时负责：

- 按日编排；
- 候选执行结果重算；
- 冻结规则/挑战模型/随机/指数/现金比较组；
- bootstrap 和日级指标；
- SQLite 读写；
- JSON 压缩、恢复和 compact response。

它还从 `strategy_validation` 导入私有 `_compute_outcome`，形成错误依赖方向。该模块应与 outcome 拆分同步处理，不能只在目标树中写“可选”。

### N6【中】`scoring_core/base.py` 是无内部消费者的私有符号 barrel

该文件大量 re-export 下划线函数，但当前工程没有直接消费者。它扩大耦合面，也会让后续 explanations 拆分更困难。处理方式：

- 先确认是否属于外部兼容 API；
- 新代码禁止从该文件导入；
- 添加 deprecation 说明，最终删除，而不是继续把新 `BaseScorer` 放进这个 barrel；
- 新基类应放在 `strategies/base.py`。

---

## 5. 修订后的目标结构

目标结构分阶段形成，不要求一次性移动所有文件：

```text
stock_analyzer/
├─ strategy_validation.py          # 稳定兼容 facade；不再放 outcome 实现
├─ validation_repository.py        # 稳定兼容 shim
├─ validation_policy.py            # 稳定兼容 shim
├─ validation/
│  ├─ facade.py                    # StrategyValidationStore 的内部实现/组合根
│  ├─ policy.py
│  ├─ schema.py
│  ├─ metrics.py
│  ├─ audit.py
│  ├─ outcomes/
│  │  ├─ models.py                 # OutcomeContext / 状态值对象
│  │  ├─ calculator.py             # dispatcher
│  │  ├─ today.py
│  │  ├─ close_auction.py
│  │  ├─ security_state.py
│  │  └─ diagnostics.py
│  └─ repositories/
│     ├─ base.py
│     ├─ signals.py
│     ├─ candidates.py
│     ├─ executions.py
│     ├─ outcomes.py
│     ├─ tuning.py
│     ├─ research.py
│     ├─ oos.py
│     ├─ predictions.py
│     └─ migrations.py
├─ portfolio_baseline.py           # 稳定 facade
├─ portfolio_baselines/
│  ├─ service.py
│  ├─ execution_dataset.py
│  ├─ groups.py
│  ├─ metrics.py
│  ├─ repository.py
│  └─ serialization.py
├─ services/
│  ├─ app_services.py              # composition root + 旧 API facade
│  ├─ context.py                   # 共享依赖/锁/缓存，不放用例业务
│  ├─ recommendations.py
│  ├─ predictions.py
│  ├─ validation.py
│  ├─ backtests.py
│  ├─ health.py
│  └─ background_workers.py
├─ strategies/
│  ├─ base.py                      # 仅稳定公共生命周期
│  ├─ today.py
│  ├─ tomorrow.py
│  └─ swing_2_5d.py
├─ scoring_core/
│  ├─ explanations/                # 原 explanations.py 转为同名 package
│  │  ├─ __init__.py               # 明确兼容导出
│  │  ├─ assembly.py
│  │  ├─ reasons.py
│  │  ├─ profile.py
│  │  ├─ committee.py
│  │  ├─ actions.py
│  │  └─ watch.py
│  └─ ...
├─ calibration/
│  ├─ objective.py
│  ├─ walk_forward.py
│  ├─ coordinate_descent.py
│  ├─ interactions.py
│  ├─ regime.py
│  ├─ meta.py
│  ├─ statistics.py
│  └─ cli.py
├─ calibrate.py                    # CLI/旧导入兼容 facade
├─ market_providers/               # 避免与 providers.py 的稳定模块名冲突
│  ├─ protocols.py
│  ├─ facade.py
│  ├─ realtime.py
│  ├─ eastmoney.py
│  ├─ sina.py
│  ├─ tencent.py
│  ├─ history.py
│  ├─ research_data.py
│  └─ status.py
├─ providers.py                    # 旧导入兼容 facade
└─ config_registry.py              # flat config 之上的动态分域视图与校验
```

选择 `market_providers/` 而不是直接把 `providers.py` 改成 `providers/`，可以在迁移期保留稳定的 `stock_analyzer.providers` 模块，并避免文件与同名 package 的导入优先级变化。

---

## 6. 分阶段实施计划

每个阶段都应拆成小 PR。除明确标为“行为修复”的成本问题外，其余结构 PR 不得改变推荐排序、执行口径、策略版本、production baseline 指纹或数据库 schema。

### 阶段 0：锁定行为、修复立即可见的正确性问题

#### PR 0.1：兼容与契约基线

实施：

1. 修复 `validation_repository.__all__` 的 `MarketGateRepository`。
2. 增加公共导入矩阵测试：
   - `stock_analyzer.strategy_validation`；
   - `stock_analyzer.validation_repository`；
   - `stock_analyzer.providers`；
   - `stock_analyzer.calibrate`；
   - `stock_analyzer.scoring_core.explanations`。
3. 保存三类 characterization fixture：
   - 三策略同一输入的 rows/meta；
   - outcome 的 settled/pending/unfilled/delisted/limit-down 分支；
   - 推荐和验证 HTTP payload 的关键字段。
4. 增加 SQLite 迁移、旧库打开、批次写入、事务回滚测试。
5. 建立 `benchmarks/` 或 `tests/performance/`：百分位、PIT 100/1000 行、rolling backtest。

验收：

- 所有 `__all__` 符号真实存在；
- 当前 fast/integration 测试通过；
- characterization fixture 可在拆分 PR 中逐字段比较；
- 不修改 `config/runtime.json` 和 `.runtime` 产物。

#### PR 0.2：显式执行成本上下文（行为修复）

实施：

1. 新增唯一公开计算入口，要求 `policy` 或合法 `strategy_name` 至少一个存在。
2. `backtest.py` 不再从 `strategy_validation` 导入私有别名，并在结果中保存成本 policy/version。
3. `paper_trading.py` 使用冻结 policy；旧信号回退到真实策略名，不再构造空策略 policy。
4. `portfolio.py`、`portfolio_baseline.py`、`validation_outcomes.py` 直接依赖 policy/cost 模块，不经 Store facade。
5. 保留 `strategy_validation._execution_cost_pct` 等兼容别名一轮，但新代码禁止使用。
6. 提升受影响的 paper/backtest 报告版本，隔离旧零成本结果。

验收：

- 可执行策略的默认成本大于 0，且低流动性成本高于高流动性；
- 同一冻结 policy 可复算出完全相同的 fee/slippage/impact/total；
- 空策略调用明确报错或只允许标注为 observation 的零成本，不再静默混用；
- 旧验证信号和数据库记录不被批量重写。

### 阶段 1：先解决已证实的热路径

#### PR 1.1：百分位 context 预排序

实施：

1. 引入不可变预排序数值容器。
2. `_score_context` 每列只清洗/排序一次。
3. `percentile_score` 对预排序容器使用二分查找，对普通 iterable 保持兼容。
4. 三策略 rows/meta 与阶段 0 fixture 深度等价。

验收：

- 排名、分数和 reasons 不变；浮点允许误差不超过 `1e-9`，最终 round 后必须完全一致；
- 3,000 次同列百分位计算相对当前基线至少提速 50 倍；
- ties、NaN、inf、空列表行为不变。

#### PR 1.2：PIT 快照批量预处理

实施顺序：

1. 在循环外对 `source_frame` 执行一次 `rename_known_columns`，删除每行 `_normalized_source_row → DataFrame([raw])`。
2. 预计算 normalized code、重复 code 保留规则、所有 hard-filter 失败 key。
3. cutoff 只解析一次；quote/event/fundamental 的公共时间只解析一次。
4. 将 candidate/selected/scored/event/fundamental 通过 code 索引对齐。
5. 行循环只负责：
   - 合并该证券嵌套 payload；
   - 生成逐字段 missing mask；
   - 生成真正逐字段的 PIT violation；
   - 组装最终 JSON-safe 记录。
6. 评估 `itertuples`/records 代替 `iterrows`，但以输出等价为前提。

验收：

- 针对重复 code、未来公告、缺失 quote timestamp、fundamental degraded、event hard exclude 的输出完全等价；
- 100 行基准至少提速 5 倍；
- 5,527 行真实形状离线基准可在可接受的快照窗口内完成，并记录峰值内存；
- snapshot hash、候选计数和 PIT 有效计数不变。

#### PR 1.3：候选过滤与 rolling 因子 panel

实施：

1. `buyable_gain` 改成按 `market` 的向量布尔 mask。
2. 新增批量数值转换，精确保留 `coerce_number` 对逗号、百分号、空串、无穷值的语义。
3. 为 rolling backtest 每只股票一次性生成按交易日索引的因子 panel；循环中只做查表和退出模拟。
4. 不在同一 PR 修改因子公式或回测成本口径。

验收：

- filter report 每种拒绝原因计数不变；
- panel 与逐窗口 `compute_alphalite_for_stock` 在全部交易日逐字段一致；
- rolling 回测 selected/trade/equity/metrics 等价；
- 在固定 35 只 × 160 日 fixture 上记录至少 3 倍提速目标，未达目标则不合并复杂实现。

#### PR 1.4：Factor IC artifact 批次缓存

实施：

- cache key 包含规范化 path 与 `mtime_ns`；
- 一次评分批次只加载一次 artifact；
- 配置 path 在测试中切换时必须立即失效；
- 文件删除、损坏、原子替换保持当前降级语义。

### 阶段 2：解除依赖倒置并拆验证核心

#### PR 2.1：清理 `strategy_validation` 的 helper hub

将非 Store 消费者改为直接依赖真实定义模块：

| 消费者 | 当前依赖 | 目标依赖 |
|---|---|---|
| `backtest.py` | `strategy_validation._execution_cost_pct` | `execution_policy` 的显式策略入口 |
| `paper_trading.py` | 多个 `strategy_validation` 私有 helper | `validation.policy` + `execution_policy` |
| `portfolio.py` | `strategy_validation.market_impact_cost_pct` | 显式 cost policy 服务 |
| `portfolio_baseline.py` | `_compute_outcome` 等私有符号 | `validation.outcomes.calculator` + `validation.policy` |
| `app_support.py` / `recommendation_runtime_support.py` | policy helper 经 Store 模块 | `validation.policy` |

增加一个架构测试，禁止业务模块新增 `from .strategy_validation import _...`。

#### PR 2.2：拆 outcome calculator

实施：

1. 建立 `OutcomeContext`，显式携带 signal、frozen policy、security state、raw history 元数据和 cutoff。
2. 将 `_compute_outcome` 变成短 dispatcher。
3. 分离：
   - today continuation；
   - close-auction / post-14:30 executable outcome；
   - security state 与 survivorship；
   - pending diagnostics；
   - entry/exit price resolution 和结果 payload assembly。
4. 共享 `risk_rules.simulate_exit`，不复制退出算法。
5. 顶层 `strategy_validation.py` 显式 re-export `_compute_outcome`、`_load_execution_history` 等已有兼容符号。

验收：

- 每个 calculator 主函数控制在约 50–80 行以内；
- 原始价格、entry/exit quantity、label status、promotion eligibility 和 return formula 完全等价；
- `portfolio_baseline` 与验证更新调用同一个 calculator；
- 不发生 import cycle。

#### PR 2.3：repository 前置去重

实施：

1. 为 `PortfolioRepository` vs `TuningRepository`、`ExperimentRepository` vs `ResearchRepository` 添加 SQL 行为等价测试。
2. 内部只保留后两者。
3. 旧类名先做 deprecated alias；无效 `MarketGateRepository` 不创建假实现。
4. 把 `PredictionRepository.update_stock_prediction_outcomes/stance_metrics` 的计算移到 `PredictionOutcomeService`，repository 只保留 CRUD/query。

#### PR 2.4：物理拆 repository

实施：

1. 新建 `validation/repositories/`，按实际数据域移动已去重类。
2. `ValidationRepository` 移到 `validation/repositories/facade.py` 或 `validation/facade.py`。
3. 旧 `validation_repository.py` 只做明确导入和 `__all__`，禁止 wildcard。
4. 保留 connection factory、row factory、事务边界和 SQL 原文；本 PR 不改 schema/索引。

验收：

- facade 的方法签名和返回结构不变；
- save signal + candidate + execution 的事务原子性不变；
- 旧路径、旧类名兼容测试通过；
- 任一 repository 文件职责单一，禁止再次出现未被 facade 使用的重复类。

#### PR 2.5：增量建立 validation package

只移动已经被改动并有契约覆盖的模块。每个旧路径保留文件 shim，例如：

```python
# stock_analyzer/validation_metrics.py
from .validation.metrics import ValidationBaselineService, ValidationMetricsService

__all__ = ["ValidationBaselineService", "ValidationMetricsService"]
```

注意：仅在 `validation/__init__.py` re-export **不能**保持 `stock_analyzer.validation_metrics` 可导入，这是原计划必须修正之处。

### 阶段 3：拆 portfolio baseline 与应用服务

#### PR 3.1：拆 `portfolio_baseline.py`

建议边界：

- `service.py`：按日运行、报告编排；
- `execution_dataset.py`：冻结候选和 outcome 重算；
- `groups.py`：规则、模型、随机、指数、现金组；
- `metrics.py`：日序列、bootstrap、percentile；
- `repository.py`：`daily_portfolio_baselines` SQL；
- `serialization.py`：压缩、恢复、compact response。

顶层 `portfolio_baseline.py` 保留稳定 facade 和函数 re-export。

验收：

- 相同 seed 的随机样本、candidate hash、baseline id 完全一致；
- legacy full JSON 与新压缩记录均可读；
- GET 不执行回填、POST 行为不变；
- 不再从 `strategy_validation` 导入私有 helper。

#### PR 3.2：让 AppServices UseCase 拥有实现

实施顺序：

1. 把 `_AppServiceContext` 拆成只含依赖/共享状态的 `ServiceContext`。
2. 按现有 UseCase 名称逐个移动方法实现，先 Health/Backtest/Prediction，再 Validation，最后 Recommendation。
3. 缓存、锁、single-flight refresh、snapshot writer 等共享设施形成明确 runtime 对象，不通过 `owner` 回调。
4. `AppServices` 继续保留所有旧 route-facing 方法，内部只转发一次。
5. 路由是否改用 `services().validation.*` 作为后续可选清理，不与实现搬迁同 PR。

验收：

- `_AppServiceContext` 不再承载业务方法；
- route status code、错误 payload、缓存命中、后台刷新去重行为不变；
- 冷启动 Web 请求不等待远程行情的契约不变；
- 测试 patch 改为定义模块路径，不依赖旧文件偶然导入的 `threading`/`datetime`。

### 阶段 4：评分器复用与解释层拆分

#### PR 4.1：小而稳定的 `BaseScorer`

仅提取：

- 依赖构造与默认实现；
- immutable scoring context；
- market filter；
- 排序后冻结 `rank/frozen_rule_rank` 的 candidate pool helper；
- 可选的公共 `score_desc + expected_return` helper（只有签名完全一致时）。

不提取：

- Tomorrow 的严格池/备选池；
- Swing 的 history factor degraded；
- Today 的 observation-only 标记；
- 各策略的 meta 和 tier 组装。

这样可以消除真实重复，同时避免脆弱的“万能模板方法”。完成后用实际 diff 统计重复减少量，不预设“30%”。

#### PR 4.2：风险规则归位

把 short-term risk penalty parts 从 `today_score.py` 移到 `risk.py`，目的是统一风险规则的定位和测试入口，不宣称三策略算法相同。三种策略各自保留独立函数和配置。

#### PR 4.3：`explanations.py` 转 package

拆分顺序：

1. reasons；
2. committee/profile；
3. risk assessment；
4. trade/exit action；
5. assembly/watch mutation。

`scoring_core.explanations.__init__` 必须 re-export现有可见符号；`ExplanationBuilder` 继续是策略层首选 API。拆分后检查 JSON 字段顺序无要求，但字段值和 reasons 顺序必须不变。

#### PR 4.4：清理 compatibility barrel

- 新代码禁止导入 `scoring_core.base`；
- 确认外部兼容窗口后删除或缩为显式 re-export；
- 不在 barrel 中继续暴露新增私有实现。

### 阶段 5：拆 calibration 与 providers

#### PR 5.1：calibration 等价拆分

按依赖从叶子向上移动：

1. statistics/FDR；
2. objective；
3. walk-forward split/evaluate；
4. coordinate descent；
5. interaction/regime/meta evaluators；
6. artifact writer；
7. CLI。

`calibrate.py` 保留 `main()`、旧导入函数和 `python -m stock_analyzer.calibrate`。现有测试大量 patch `stock_analyzer.calibrate.*`，实施时应：

- 行为测试改 patch 定义模块；
- 另保留公开导入兼容测试；
- 不在拆分 PR 新增 Ridge、LightGBM 或改变 FDR 口径。

#### PR 5.2：统一 weights artifact reader

建立一个只负责原子读取、schema 检查、production freeze 选择和默认值回退的 reader；`alphalite_signal`、策略 `weights`、`thresholds` 仍使用各自 schema。避免把不同 section 粗暴合成一个 dict。

#### PR 5.3：provider 内部组件化

实施顺序：

1. 提取 `TimedCache`、`ProviderStatus`；
2. 提取 Eastmoney/Sina/Tencent 纯请求和规范化函数；
3. 提取 history store/cache/fetch；
4. 提取 news/event/fundamental research data；
5. `MarketDataProvider` 变成组合 facade。

使用 `Protocol` 描述最小能力：`QuoteProvider`、`HistoryProvider`、`ExecutionBarsProvider`、`ResearchDataProvider`、`SecurityStateProvider`。不要要求测试中的轻量 FakeProvider 继承抽象基类。

验收：

- fallback 顺序、timeout、coverage 检查、attrs 元数据和 health 计数不变；
- Web nonblocking 和后台 refresh single-flight 不变；
- `stock_analyzer.providers.MarketDataProvider/TimedCache/ProviderStatus` 旧导入继续工作；
- 网络测试继续使用 fake session，不新增真实网络依赖。

### 阶段 6：配置分域与收尾迁移

#### PR 6.1：配置 schema 和动态 domain view

先不改 `runtime.json` 形状，新增：

- 配置 key → domain/type/default/secret policy 注册表；
- 启动时未知 key、错误类型、tuple/list 规则校验；
- `config_registry.scoring`、`.validation`、`.portfolio` 等动态 view。

view 必须动态读取 flat `config`，确保现有 `patch.object(config, ...)` 测试和运行时 override 可见；不建议用启动时快照的 `MappingProxyType`。

#### PR 6.2：消费者渐进迁移

新代码使用 domain view；旧代码继续支持 `config.X`。按模块迁移并减少 `getattr(..., default)`，但每次只迁一个域。

#### PR 6.3：可选 JSON schema v2

只有在：

- 所有 domain view 已覆盖；
- production baseline 指纹规则明确；
- schema v1 → v2 有确定迁移和回滚；
- 文档、CLI、测试均更新；

之后才把 `runtime.json.settings` 改成嵌套结构。该变更必须独立 PR，并生成新 production baseline；不得与普通重构混合。

---

## 7. 兼容、数据库与生产冻结约束

### 7.1 导入兼容

兼容分两层：

1. **公共导入兼容**：旧模块路径和公开类/函数继续可导入，由显式 shim 保证。
2. **测试 patch 路径**：patch 应指向符号的定义模块。旧模块只 re-export 时，patch `stock_analyzer.providers.threading.Thread` 不会自动影响新实现；这类测试要迁移，不能靠 shim 假装兼容。

所有 shim 必须显式列 `__all__`，禁止 `from ... import *`。

### 7.2 SQLite 约束

结构重构阶段：

- 不修改表、列、索引和 migration id；
- 不改变 `sqlite_transaction` 的 commit/rollback 行为；
- 不把原本同事务的多表写入拆成多个连接；
- repository 拆分前后对同一临时库做行级 dump 比较；
- schema 变更必须另开 PR，继续由 `ValidationSchemaManager` 幂等迁移。

### 7.3 生产冻结约束

纯移动/重命名不得改变：

- strategy version；
- execution policy version；
- validation baseline id；
- production baseline manifest；
- 推荐排序、tier、execution_allowed；
- snapshot hash 的业务输入。

成本零值修复和未来 config schema v2 属于行为变化，必须单独版本化；旧样本不混入新基线。

### 7.4 回滚策略

每个 PR 必须满足：

- 兼容 facade/shim 尚在；
- 无数据库不可逆迁移；
- 新实现能通过单一 import 或 feature flag 切回旧实现（只在高风险双跑期使用）；
- 不提交 `.runtime`、本地 SQLite、缓存和生成 artifact；
- PR 只处理一个边界，避免“移动文件 + 改算法 + 改 schema”同时发生。

---

## 8. 测试与验收矩阵

| 变更类型 | 必跑测试 | 专项验收 |
|---|---|---|
| 兼容 shim / 模块移动 | fast + import contract | 旧路径和 `__all__` 全通过 |
| execution cost | fast + integration + cost scenario | 策略/policy 显式；冻结 policy 可复算；版本隔离 |
| outcome 拆分 | fast + integration + slow outcome | settled/pending/unfilled/delisted/limit 分支等价 |
| repository 拆分 | fast + integration + migration | 事务回滚、旧库打开、SQL 行级等价 |
| AppServices 拆分 | fast + integration + frontend contract | status/payload/cache/background single-flight 不变 |
| scorer/explanations | scoring 全集 + frontend contract | rows/meta/reasons/tier 深度等价 |
| PIT/percentile/backtest 性能 | correctness + benchmark | 相对提速达标，峰值内存无异常，输出等价 |
| providers | provider fallback + Web background | 无真实网络；fallback/health/attrs 等价 |
| calibration | calibration + CLI smoke | walk-forward split、FDR、artifact 完全等价 |
| config | runtime_json + production_freeze | v1 兼容、patch 可见、指纹规则明确 |

推荐每个阶段结束至少执行：

```bash
scripts/test.sh fast
scripts/test.sh integration
scripts/test.sh slow
scripts/test.sh slow-integration
python -m stock_analyzer.calibrate --help
python -m stock_analyzer.jobs --help
```

性能测试应记录 Python、pandas、CPU、输入行数、预热次数、中位数和峰值内存。禁止只报告单次最好结果，也禁止用“预计提升 40–70%”代替基准数据。

---

## 9. 建议的合并顺序与完成定义

最终建议顺序：

1. PR 0.1 兼容/characterization/benchmark 基线；
2. PR 0.2 成本显式上下文和版本隔离；
3. PR 1.1 百分位预排序；
4. PR 1.2 PIT 批处理；
5. PR 1.3 候选过滤与 rolling panel；
6. PR 1.4 Factor IC 缓存；
7. PR 2.1 解除 `strategy_validation` 私有 helper 依赖；
8. PR 2.2 outcome calculator；
9. PR 2.3 repository 去重；
10. PR 2.4 repository 物理拆分；
11. PR 2.5 validation package 增量迁移；
12. PR 3.1 portfolio baseline 拆分；
13. PR 3.2 AppServices 真正按用例拆分；
14. PR 4.x scorer/explanations；
15. PR 5.x calibration/providers；
16. PR 6.x config 与收尾。

某项只有同时满足以下条件才算完成：

- 新边界拥有明确职责和最小依赖；
- 原行为或明确版本化后的新行为有自动化测试；
- 旧公共导入路径在兼容窗口内可用；
- 没有新增 facade 反向依赖和循环导入；
- 性能项有复现基准，结构项有行级/payload 等价证据；
- 文档中的模块职责同步更新；
- 工作区不包含运行时产物和无关文件改动。

---

## 10. 本轮明确不做的事项

- 不因拆 `calibrate.py` 顺便新增 Ridge、LightGBM 或改变 FDR 统计口径；
- 不引入 `cvxpy`/`scipy` 替换当前小规模权重投影；
- 不向量化有顺序依赖的 `simulate_exit`；
- 不一次性移动全部 `validation_*.py`；
- 不删除 `strategy_validation.py`、`providers.py`、`calibrate.py` 等稳定入口；
- 不在纯重构 PR 修改策略权重、阈值、执行规则、冻结 manifest 或数据库 schema；
- 不用测试全绿证明收益有效；本计划只解决工程边界、正确性和运行性能，不替代策略 OOS 验证。

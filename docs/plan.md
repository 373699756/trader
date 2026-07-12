# 荐股与评分科学化优化计划

> 更新日期: 2026-07-12
> 目标: 只保留能够提高样本外净收益、降低回撤或提高收益证据可信度的机制
> 适用范围: `tomorrow_picks`、`swing_picks`、验证系统、组合与 DeepSeek 复核

## 一、结论

当前系统的主要问题不是评分因子不够多，而是同时存在综合分、预期收益分、概率分、Meta-Label、事件分、集成分和 DeepSeek 分，却没有足够真实样本证明其中任何一项能增加收益。截至 2026-07-12 审计时，`.runtime/strategy_validation.sqlite3` 中信号和结果均为 0；现有 20/30/60 样本门槛也不足以支持多模型选择。

因此采用以下结论:

1. **不再把“评分更复杂”视为优化。** 唯一生产目标是相对可交易基准的样本外组合净收益；Sortino、最大回撤、换手和容量是约束。
2. **先修真实标签与基准，再训练模型。** 成交价格、成本、不可成交、退市、停牌和策略版本未固定前，所有收益对比均无效。
3. **只保留一个生产排序。** 初期保留当前规则分作为候选基线；有足够数据后，只允许一个预期净收益模型挑战它。
4. **概率不是第二套模型。** `p_win` 必须由同一个收益模型派生并做 OOS 校准；旧分数分桶概率不参与交易。
5. **复杂组件默认删除生产作用。** 因子交互、分状态模型、Meta-Labeling、手工事件加分、固定权重 Ensemble、DeepSeek 加权在证明独立增量前全部保持关闭；没有数据采集价值的展示字段直接删除。
6. **先做明日策略。** `tomorrow_picks` 标签清晰、成熟快；`swing_picks` 在明日策略闭环稳定后复用流程；`short_term` 继续作为零仓位观察，不纳入收益优化。
7. **不承诺收益。** 任何机制只有历史样本外证据，没有未来收益保证；生产系统必须保留空仓和自动回退能力。

## 二、科学评价标准

### 2.1 决策单位与标签

生产排序和验证必须完全复现可执行交易，而不是评价单只股票的理论涨跌。

| 项目 | `tomorrow_picks` | `swing_picks` |
|---|---|---|
| 信号时点 | 交易日收盘快照，固定版本 | 交易日收盘快照，固定版本 |
| 入场 | 次日开盘可成交价；一字涨停等不可买入样本记为未成交 | 同左 |
| 退出 | 次日收盘 | 固定且版本化的 2-5 日退出规则 |
| 主标签 | 组合净收益 | 组合净收益 |
| 净收益 | 毛收益 - 双边费用 - 滑点 - 冲击成本 | 同左 |
| 风险标签 | MAE、最大回撤、尾部损失 | 同左 |
| 基准 | 同候选池等权 Top-K、沪深 300/中证 1000、现金 | 同左 |

要求:

- 交易成本必须按资金规模、成交额和参与率做低/中/高三档敏感性分析，不能只报告单个假设。
- 同日多只股票不是独立样本。统计检验以“交易日组合收益”为单位，股票数只用于描述覆盖率。
- 训练特征必须是信号时点可见数据；公告使用真实发布时间，盘后公告不能进入当日信号。
- 标签、候选池、成本参数、策略版本和模型版本共同生成不可变 `baseline_id`；不同 `baseline_id` 禁止混算。
- 不可成交不是普通亏损样本，也不能静默删除；单独记录未成交率、原因及机会成本。

### 2.2 唯一主指标与约束

主检验指标:

```text
daily_incremental_net_return
  = 新方案每日等权 Top-K 净收益 - 冻结基线每日等权 Top-K 净收益
```

生产晋级必须同时满足:

- OOS 日均增量净收益为正，且按交易日 block bootstrap 的 95% 置信区间下界大于 0。
- 扣除多重试验后仍通过；候选方案少时用 Holm，持续研究时用 Benjamini-Hochberg FDR。
- OOS Sortino 不下降，最大回撤恶化不超过预设容忍度，换手和容量约束通过。
- 至少 60 个 OOS 交易日用于首次灰度、120 个 OOS 交易日用于正式替换；每个关键市场状态至少 20 日，否则只使用全局模型。
- 结果不能由单月、单股或单一行业贡献；去掉最佳 5 个交易日后仍不为负。
- 高成本情景下仍不显著劣于基线。

`胜率`、`平均单股收益`、`综合分`、`IC`、`AUC`、`解释文本质量`均为诊断指标，不能单独触发上线。

### 2.3 时序验证协议

统一使用 anchored walk-forward，并按持有期执行 purge/embargo:

1. 训练集只使用测试窗口开始前已经成熟的标签。
2. `tomorrow_picks` purge 至少 1 个交易日；`swing_picks` purge 至少最大持有期 5 个交易日。
3. 超参数选择仅在训练/验证窗口完成；最终测试窗口只评估一次。
4. 所有候选模型在完全相同的日期、候选池、Top-K、成本和缺失样本上做配对比较。
5. 保存每折预测，而不只保存汇总值，以便重算、审计和反事实比较。
6. 每次新增因子或阈值都登记为一次试验；禁止反复查看测试集后继续调参。

## 三、机制去留审计

### 3.1 保留

| 机制 | 决定 | 原因 | 生产边界 |
|---|---|---|---|
| 候选资格硬过滤 | 保留 | ST/退市风险、停牌、无法买入、流动性和数据缺失是可交易约束 | 只负责资格，不伪装成 alpha |
| 当前规则分 | 暂时保留 | 在无数据时作为冻结基线和候选压缩器 | 不解释为概率或预期收益 |
| 真实成交与成本模型 | 必须保留 | 防止虚假收益，决定策略容量 | 做参数敏感性和实盘成交校准 |
| 幸存者/停牌处理 | 必须保留但重做语义 | 防止静默丢失坏样本 | 不得用统一默认亏损冒充真实退市成交 |
| 时间衰减 | 有漂移证据后保留 | 可能适应结构变化 | 半衰期作为少量预注册候选，不默认等于 60 日 |
| 单票、行业、相关性和容量上限 | 保留 | 降低集中风险和不可成交风险 | 作为约束，不宣称产生 alpha |
| 波动率目标和回撤降仓 | 保留 | 可改善风险调整收益 | 与固定仓位做独立组合级消融 |
| 压力测试和监控 | 保留 | 防止尾部风险及数据故障 | 是上线门槛，不是收益来源 |
| DeepSeek 结构化事件抽取 | 有条件保留 | 可形成带时间戳的研究特征 | 只采集/复核，未验证前不改排序和动作 |

### 3.2 删除生产作用或合并

| 当前机制 | 处理 | 依据 |
|---|---|---|
| `ScoreCalibrator` 分数分桶概率 | 删除交易作用 | 20 个样本分 5 桶统计功效不足；概率与收益模型重复。页面若保留，只显示 OOS 校准值和样本数 |
| 启发式 `expected_return` fallback | 删除展示和交易作用 | 它由原始 `score` 手工换算，不是期望收益，容易产生伪精确 |
| k 近邻式预期收益 + 手工 `rank_score` 权重 | 替换 | 距离尺度和 6/40/2/0.8/1.4 权重无收益估计依据，且近邻样本高度相关 |
| 坐标下降权重调优 | 冻结 | 搜索空间粗糙且重复使用样本；作为基线，不再持续自动调权 |
| 二阶因子交互 | 删除当前生产路线 | 小样本下高过拟合；未来只能作为单个挑战模型的一部分，不设独立开关 |
| 分市场状态独立模型 | 暂缓 | 状态切片进一步稀释样本；先把状态作为一个特征，样本充分后再做消融 |
| Meta-Labeling | 删除当前路线 | 与 `p_win`/预期收益和仓位降权职责重复，形成第三套决策器 |
| 固定权重 Ensemble | 删除当前路线 | 混合的是尺度不同且相互派生的分数，不是独立模型，也没有 OOS 学得权重 |
| 手工事件类型权重与事件加分 | 删除生产作用 | 规则权重是主观常数；保留事件原始字段，等待事件时间序列消融 |
| DeepSeek `bonus/penalty/rerank/veto` | 默认关闭生产影响 | 模型输出不稳定且没有反事实收益证据；只允许安全类硬风险提示 |
| Max-Sharpe 优化 | 暂不实施 | 小样本协方差和收益估计误差会放大；先用等权加简单上限 |
| 盘中观察收益建模 | 删除计划 | 当前为零仓位观察，缺少分钟级可执行标签 |
| 固定“预计提升 X%” | 全部删除 | 在无真实样本时属于无依据承诺 |

### 3.3 因子保留规则

对现有每个因子执行 leave-one-feature-out 消融，不凭金融故事保留。因子进入生产必须满足:

- 覆盖率不低于 95%，缺失值不能通过未来数据回填。
- 在多数 OOS 折中方向稳定，且去掉该因子后 Top-K 增量净收益下降。
- 加入该因子后组合净收益的 bootstrap CI 改善，或在净收益不降时明显降低回撤/换手。
- 与已有因子高度冗余时只保留更稳定、便宜、及时且易解释的一项。
- 连续两个 60 日监控窗口无边际贡献或方向反转，自动降为 shadow；再持续一个窗口仍无贡献则删除。

优先审计顺序:

1. 流动性、可买入性、价格限制: 归入资格/成本层，不参与 alpha 归因。
2. 中短期动量、相对行业/市场强度、量价确认: 作为首批 alpha 候选。
3. 过热、振幅、波动率、回撤: 归入风险或收益模型特征，避免在过滤、扣分和仓位中重复惩罚。
4. 尾盘结构: 仅在 14:30 后特征完整时参与明日模型；盘中不得填充中性值后假装完整。
5. 基本面与事件: 必须 point-in-time 对齐后再进入研究，无法确认发布时间的字段删除。

## 四、目标架构

### 4.1 五层决策链

```text
全市场快照
  -> 资格层: 数据完整、可买入、流动性、风险黑名单
  -> 基线层: 当前规则分压缩到候选池
  -> 收益层: 一个模型输出 E[净收益]、下行分位、p_win、置信区间
  -> 组合层: 等权起步 + 单票/行业/相关性/容量/波动约束 + 允许现金
  -> 执行与验证: 次日真实成交、费用、滑点、未成交和逐日 OOS 归因
```

每层只承担一个职责。禁止同一风险在过滤、综合分、Meta-Label、DeepSeek 和仓位中被重复扣除。

### 4.2 唯一生产排序

阶段 A 没有合格模型时:

```text
ranking_key = frozen_rule_score
```

阶段 B 模型通过晋级后:

```text
ranking_key = predicted_net_return
```

`p_win`、`downside_q10` 和预测区间用于解释、拒绝低置信交易及组合约束，不再手工拼成第二个 `0-100 rank_score`。页面同时展示原始规则分和模型预测，但明确标注哪个字段实际决定排序。

### 4.3 首个挑战模型

首个模型只使用稳健、低自由度方案:

- 目标: `primary_return_net`，同时报告 Huber/分位损失下的稳健性。
- 特征: 先经去重和 point-in-time 审计的 8-12 个连续特征；类别特征严格限维。
- 候选: 正则化线性模型作为首选；非线性模型只有在线性模型 OOS 稳定后才挑战。
- 概率: 从同一 OOS 预测派生 `P(net_return > 0)`，用 Brier score、校准斜率和 reliability bins 评估。
- 不确定性: 使用折间分散度和 bootstrap 预测区间；低覆盖或分布外样本退回基线/空仓。
- 不自动在线调参；按固定月度或 20 个新交易日再训练，模型 artifact 记录训练截止日和完整配置。

## 五、完整实施计划

### P0: 冻结生产与建立试验登记（1-2 天）

- 记录所有当前开关、策略版本、权重、Top-K、候选过滤和退出规则，生成冻结基线清单。
- 关闭生产影响: `ENABLE_EXPECTED_RETURN_RANKING=0`、`ENABLE_INTERACTION_TERMS=0`、`ENABLE_REGIME_SPECIFIC_WEIGHTS=0`、`ENABLE_META_LABELING=0`、`META_LABELING_ENFORCE_ACTION=0`、`ENABLE_EVENT_ALPHA=0`、`ENABLE_ENSEMBLE=0`；DeepSeek 仅 shadow。
- 建立试验登记表: 假设、唯一变更、训练窗口、测试窗口、主指标、风险约束、试验族、结果和决定。
- 固定 `tomorrow_picks` 为首个研究策略，Top-K 初始取 5；另报告 K=3/10 的敏感性，不挑最好 K 上线。

验收: 任意推荐可回答“由哪个版本、哪个排序字段、哪些开关生成”，且同输入可重放。

### P1: 修复 point-in-time 数据与交易标签（3-7 天）

- 为信号保存当时完整候选池及未入选候选，不能只保存 Top-K；否则无法离线重排和做公平反事实。
- 保存原始特征值、缺失掩码、数据源时间戳、公告发布时间、行情截止时间、候选资格原因。
- 将入场/退出、涨跌停、停牌、未成交、费用、滑点和冲击成本写成版本化 execution policy。
- 将“退市默认 -30%”改为状态化处理: 能取最后可交易价则按可实现清算价；数据源故障记 `unknown` 并阻止晋级，不可伪造收益。
- 记录组合资金、目标权重、实际成交量/价、未成交量；暂时没有真实成交时保留低/中/高成本情景。
- 增加市场、行业和风格基准的同周期收益。

验收: 抽查至少 30 个信号，所有特征在当时可见；收益能从原始价格逐笔复算；缺失、未成交和退市样本守恒。

### P2: 建立日级组合基线（3-5 天）

- 每日对完整候选池运行冻结规则，生成等权 Top-5 纸面组合，并以次日可成交规则结算。
- 同时生成四个比较组: 当前规则 Top-K、合格候选等权随机抽样、主要指数、现金。
- 随机基准固定随机种子并重复至少 1,000 次，报告规则相对候选池随机选择的分位。
- 输出每日净收益、累计收益、回撤、Sortino、换手、行业集中、容量利用率和未成交率。
- 将纸面组合与页面展示、验证库和 OOS 报告对齐，消除“单股指标好但组合亏损”的口径差异。

验收: 基线可以每日自动运行、重放和审计；任何模型均能在同一批日期上配对比较。

### P3: 积累样本并审计现有规则（至少 60 个交易日，可并行）

- 前 20 日只检查数据和执行质量，不做收益结论。
- 20-60 日做 shadow 诊断: 分数单调性、因子覆盖、相关性、稳定性、Top-K 相对随机基准表现。
- 使用按日 RankIC、分层收益和 leave-one-feature-out 分析现有因子；不根据单次结果改生产。
- 建立风险重复矩阵，合并多处重复的过热、振幅、风险和流动性惩罚。
- 分别报告开盘高开过滤、不可买入过滤和成本模型对收益的贡献，区分“避免假交易”与“产生 alpha”。

验收: 至少 60 个真实、同版本、当前 `baseline_id` 的成熟交易日；数据覆盖率和失败率达到 P1 标准。

### P4: 训练一个预期净收益挑战模型（5-10 天）

- 建立特征 manifest，先删除泄漏、重复、低覆盖和无法 point-in-time 对齐的特征。
- 使用 P2 协议执行 purged walk-forward；所有预处理在每个训练折内拟合。
- 比较仅三项: 冻结规则分、等权低维线性模型、正则化线性收益模型。暂不加入交互、Meta 或 Ensemble。
- 对每日 Top-K 生成完整 OOS 预测，计算相对冻结基线的配对增量净收益和 bootstrap CI。
- 通过消融确定特征去留；把所有尝试纳入 Holm/FDR，而不是只对获胜模型检验。
- 只有达到 60 OOS 日且所有门槛通过，才生成带过期时间的 shadow artifact。

验收: 训练脚本可从空目录重建 artifact；测试折预测与汇总可追溯；未通过时明确结论为“基线胜出”，不继续堆模型。

### P5: 组合和仓位独立消融（5-7 天）

按固定顺序一次只增加一个机制:

1. 等权 + 单票上限。
2. 加行业/主题上限。
3. 加真实相关性上限，相关性必须来自截至信号日的历史收益，不使用主题名代替相关性。
4. 加容量约束和现金仓位。
5. 加波动率目标。
6. 加回撤降仓。

每一步同时与固定满仓和上一步比较。若只降低波动但按目标风险重新缩放后没有改善 Sortino/回撤，记录为风险偏好选项，不宣称提高 alpha。

验收: 组合层不会用 `low/shadow` 预测加仓；所有权重之和、现金、cap 和成交容量可解释并有属性测试。

### P6: DeepSeek 与事件特征的受控试验（累计至少 60 个事件日）

- DeepSeek 只读取本地 Top-N 和 point-in-time 新闻/公告，输出结构化事件类型、时间戳、来源、置信度和风险标志。
- 不再让 LLM 直接输出最终排名；将结构化字段作为单独挑战特征，在相同 OOS 日期上比较“基础模型”与“基础模型 + 事件”。
- 保存调用前候选、输出、模型名、prompt 版本、token、延迟和失败结果；缓存相同证据哈希。
- 安全类否决只限明确规则，如退市公告、监管立案、无法交易；主观情绪不得硬否决。
- 用增量净收益、避免亏损、误杀盈利、调用成本和覆盖偏差评估；没有正增量时关闭生产调用，仅保留人工研究。

验收: block bootstrap/FDR 通过且扣除 token 等价成本后仍有正增量，才允许 10% shadow-to-live 灰度。

### P7: 灰度、上线与自动回退（至少 20 个灰度交易日）

- 第 1 阶段 shadow: 页面可看，不改变排序/仓位。
- 第 2 阶段 10% 确定性流量: 按日期/账户哈希分配，不允许人工挑日。
- 第 3 阶段 30%-50%: 连续 20 个灰度日通过后扩大。
- 正式替换: 至少 120 个 OOS 日、两种以上市场状态、扣成本增量 CI 为正。
- 自动回退条件: 数据覆盖下降、artifact 过期、特征漂移超阈值、20 日增量净收益 CI 明显为负、回撤超限或成本/未成交率异常。
- 回退到冻结规则基线；基线本身未通过健康门控时退到现金，而不是强制荐股。

验收: 关闭一个开关即可恢复基线；回退演练不丢失信号、结果和模型归因。

### P8: 扩展到波段策略（明日策略稳定后）

- 复用 P1-P7，但标签、purge、退出与成本单独版本化。
- 不直接复用明日模型权重；只允许复用数据和验证框架。
- 因 5 日标签重叠和样本成熟更慢，正式晋级要求至少 120 个 OOS 日。
- `short_term` 保持观察池，除非另建分钟级成交、延迟、冲击和退出标签。

## 六、多 Codex 并行实施方案

### 6.1 并行原则与推荐规模

推荐同时运行 **4 个 Codex**，而不是按文件数量无限增加实例:

- `C0` 为协调、契约、审查和集成负责人。
- `C1-C3` 为写代码的领域负责人。
- 任一时刻一个文件只能有一个写入负责人；其他 Codex 可以读取，但不得顺手修改。
- `C0` 不在同一波次抢写领域文件，只做共享配置、运行接线、前端、文档和合并。
- 每个 Codex 使用独立 branch + worktree；禁止多个 Codex 同时在 `/home/cp/Public/trader` 工作。
- 每个波次开始时使用同一个集成 SHA；只在波次门控通过后同步下一版本。
- 多 Codex 只能缩短工程开发时间，不能缩短 60/120 个真实 OOS 交易日和 20 个灰度交易日。

不建议超过 4 个实例。该项目的核心冲突点集中在验证 schema、repository、`strategy_validation.py`、`config.py` 和运行接线；继续拆分会让接口协调成本大于并行收益。

### 6.2 角色和永久文件所有权

| Codex | 领域 | 独占修改范围 | 测试文件规则 | 禁止事项 |
|---|---|---|---|---|
| `C0-INTEGRATOR` | 契约、配置、生产排序接线、API、前端、DeepSeek 集成、文档 | `docs/`、`README.md`、`config.py`、`app*.py`、`app_*support.py`、`recommendation_runtime_support.py`、`validation_runtime_support.py`、`snapshot.py`、`candidate_pipeline.py`、`scoring.py`、`scoring_core/`、`strategies/`、`routes/`、`templates/`、`static/`、`deepseek/`、`deepseek_*.py` | 拥有前端、API、运行时和生产策略集成测试；优先新增独立测试文件 | 不替 C1-C3 修改领域实现；不在集成时顺手重构 |
| `C1-DATA` | point-in-time 数据、完整候选池、数据库 schema/repository、迁移、审计与重放 | `point_in_time.py`、`fundamentals.py`、`event_risk.py`、`validation_audit.py`、`validation_schema.py`、`validation_repository.py`、`validation_replay.py`、`validation_backup.py`、`recommendation_snapshot.py` | 新增 `test_point_in_time_contracts.py`、`test_validation_schema_contracts.py` 等数据测试 | 不修改收益算法、组合算法、`config.py` 或运行接线 |
| `C2-EXECUTION` | 执行规则、标签结算、成本、纸面组合、基准、容量和仓位 | `execution_policy.py`、`strategy_validation.py`、`validation_outcomes.py`、`validation_benchmarks.py`、`paper_trading.py`、`portfolio.py`、`risk_rules.py` | 新增 `test_execution_policy_contracts.py`、`test_daily_portfolio_baseline.py`、`test_portfolio_ablation.py` | 不修改 schema/repository；所需 DB 接口交给 C1 |
| `C3-RESEARCH` | 日级指标、时序 OOS、统计检验、因子消融、唯一收益模型 | `calibrate.py`、`expected_return_model.py`、`probability_calibration.py`、`validation_metrics.py`、`validation_services.py`、`validation_cache.py`、`oos_report.py`、`performance.py`、`factor_ic.py`、`stress_scenarios.py` | 新增 `test_purged_walk_forward.py`、`test_daily_incremental_metrics.py`、`test_expected_return_artifact.py` | 不修改生产排序接线、schema/repository、组合或 `config.py` |

补充规则:

- `config.py` 永远由 `C0` 修改。C1-C3 在交接中提交“配置名、默认值、用途、回退值”，由 C0 统一落地。
- 现有大型混合测试文件默认只读。每个 Codex 优先新增自己拥有的测试文件，确需修改旧测试时先由 C0 授予该波次的临时文件租约。
- `meta_labeling.py`、`ensemble.py`、`event_alpha.py` 的生产接线由 C0 移除；C3 只评估是否还有研究保留价值。
- 跨领域接口只能在波次开始的契约冻结阶段变更；波次中发现缺口时，Codex 应停止越界修改并在交接中提出接口请求。

### 6.3 当前工作区的特殊处理

截至 2026-07-12 本次复核时，工作区至少包含以下未提交 P1 方向改动；真正启动 W0 时必须重新运行 `git status --short` 和 `git diff --stat`，以当时结果为准:

- C0 范围: `docs/plan.md`、`docs/production_freeze.md`、`config/`、`experiments/`、`config.py`、`production_baseline.py`、`experiment_registry.py`、`snapshot.py`、`strategies/tomorrow.py`。
- C1 范围: `point_in_time.py`、`fundamentals.py`、`event_risk.py`、`validation_audit.py`、`validation_schema.py`、`validation_repository.py`。
- C2 范围: `execution_policy.py`、`strategy_validation.py`、`validation_outcomes.py`、`validation_benchmarks.py`。

这些改动不能通过 `stash`、覆盖或重新生成来处理。创建 worktree 前由 C0 完成一次串行检查:

1. 通知当前所有写入者在完成手头原子编辑后暂停；等待 30 秒并连续两次确认 `git status --short` 与 `git diff --stat` 不再变化。
2. 在保留当前改动的前提下创建并切换到 `integration/scientific-ranking`，不使用强制切换或清理命令。
3. 逐文件确认来源、完成度和最终负责人；不回退任何未知改动。跨负责人文件按最终所有权拆成独立 commit，不把整批脏改动交给一个 Codex 重写。
4. 运行 `git diff --check` 和相关最小测试。
5. 将已验证的当前改动形成集成分支上的基线提交；若尚未完成，先由对应负责人串行完成最小闭环再提交。
6. 创建 `docs/codex-progress.md` 进度账本，由 C0 独占更新 task、owner、base SHA、branch、commit、test 和 gate 状态。
7. 记录基线 SHA，所有 Codex 从该 SHA 创建工作分支。
8. `.runtime/`、SQLite、缓存、模型 artifact 和真实 token/密钥不得进入提交。

在这个基线提交完成前，禁止启动 C1-C3 写代码，否则独立 worktree 看不到当前未提交内容，并会产生两套冲突实现。

### 6.4 Worktree 与启动方式

先在当前工作区创建集成分支，检查并提交基线:

```bash
cd /home/cp/Public/trader
git switch -c integration/scientific-ranking
```

如果该分支已由本计划创建，使用 `git switch integration/scientific-ranking`，不要重复创建或强制覆盖。C0 完成 6.3 的检查与基线提交后，再执行:

```bash
cd /home/cp/Public/trader
BASE_SHA=$(git rev-parse HEAD)

mkdir -p /home/cp/Public/trader-codex
git worktree add /home/cp/Public/trader-codex/c1-data -b codex/c1-data "$BASE_SHA"
git worktree add /home/cp/Public/trader-codex/c2-execution -b codex/c2-execution "$BASE_SHA"
git worktree add /home/cp/Public/trader-codex/c3-research -b codex/c3-research "$BASE_SHA"
```

在四个终端分别启动。使用常规 sandbox/approval 配置，不使用 `--dangerously-bypass-approvals-and-sandbox`:

```bash
codex -C /home/cp/Public/trader --no-alt-screen "你是 C0-INTEGRATOR，按 docs/plan.md 第六章协调、审查和集成；只修改 C0 所有文件。"
codex -C /home/cp/Public/trader-codex/c1-data --no-alt-screen "你是 C1-DATA，只执行当前波次的数据契约和存储任务，严格遵守 docs/plan.md 6.2 的文件所有权。"
codex -C /home/cp/Public/trader-codex/c2-execution --no-alt-screen "你是 C2-EXECUTION，只执行当前波次的执行、标签和组合任务，严格遵守 docs/plan.md 6.2 的文件所有权。"
codex -C /home/cp/Public/trader-codex/c3-research --no-alt-screen "你是 C3-RESEARCH，只执行当前波次的指标、验证和模型任务，严格遵守 docs/plan.md 6.2 的文件所有权。"
```

非交互批处理可使用 `codex exec -C <worktree> -o <handoff-file> "<任务卡>"`。只允许把交接输出写到各自 worktree 或 `/tmp`；不要让多个实例写同一个日志文件。

如果某个实例需要运行 Flask，固定端口为 `C0=5000`、`C1=5101`、`C2=5102`、`C3=5103`。测试数据库和 `.runtime/` 必须位于各自 worktree，禁止共享正在写入的 SQLite。

### 6.5 波次开始前冻结的五个契约

W0 由 C0 组织 C1-C3 只读评审，冻结以下 JSON 可序列化契约。字段只能新增可选项，不能在波次中改名或改变语义:

| 契约 | 生产者 | 消费者 | 最小字段 |
|---|---|---|---|
| `CandidateSnapshotBatch` | C1 | C2、C3、C0 | 策略/版本、信号日期时间、行情截止时间、完整候选、资格原因、是否入选、原始特征、缺失掩码、来源时间戳、point-in-time 状态 |
| `ExecutionPolicy` / `ExecutionRecord` | C2 | C1 持久化、C3 评估、C0 展示 | policy version、入退场状态、未成交原因、目标/实成交数量和价格、费用/滑点/冲击、毛/净收益、成本情景、原始价格、是否可用于晋级 |
| `DailyPortfolioEvaluation` | C2 | C3、C0 | 日期、策略、排序源、Top-K、权重、现金、毛/净收益、换手、集中度、容量、未成交率、市场/行业基准 |
| `FoldPrediction` | C3 | C1 持久化、C0 审计 | experiment/fold ID、训练截止日、测试日期、代码、基线分、预测净收益/概率、是否入选、真实净收益、模型版本 |
| `ModelArtifact` | C3 | C0 生产门控 | baseline ID、feature schema hash、训练截止日、失效时间、特征清单、超参数、逐折摘要、OOS/FDR/CI gate、artifact hash |

契约文档和契约测试由 C0 管理。C1-C3 如需变更，提交一段兼容性说明和迁移方案，由 C0 在下一波次统一修改。

### 6.6 总体并行依赖图

```text
W0 契约/基线（串行，C0 主导）
  -> [W1-C1 数据快照 || W1-C2 执行标签 || W1-C3 纯统计内核]
  -> G1 数据与标签门控
  -> [W2-C1 查询重放 || W2-C2 日级组合 || W2-C3 OOS 报告 || W2-C0 API/页面]
  -> G2 端到端基线门控
  -> [W3-C1 数据质量 || W3-C2 组合消融框架 || W3-C3 因子审计框架 || W3-C0 旧模型脱钩]
  -> G3 至少 60 个真实 OOS 交易日（自然时间，不可并行压缩）
  -> [W4-C1 冻结数据集 || W4-C2 组合消融 || W4-C3 收益模型 || W4-C0 事件/DeepSeek 受控试验]
  -> G4 shadow 晋级门控
  -> [W5-C1 审计 || W5-C2 实盘组合归因 || W5-C3 漂移监控 || W5-C0 灰度/回退]
  -> G5 20 个灰度日 + 120 个 OOS 日
  -> W6 按同一分工扩展 swing_picks
```

### 6.7 各波次完整任务卡

#### W0: 基线、契约和协作准备（串行，1 天）

`C0`:

- 接管并审查 6.3 中已有未提交改动。
- 固化开关、策略版本、Top-K、退出规则和 `baseline_id`。
- 冻结 6.5 的五个契约，建立 experiment ID 命名和交接模板。
- 创建集成分支和三个 worktree，记录共同 `BASE_SHA`。

`C1-C3`:

- 只读评审契约和自己的现有实现，不修改文件。
- 列出接口缺口、测试入口和配置请求。

`G0` 门控:

- 当前改动已明确归属并形成可测试基线。
- 所有 worktree `git status --short` 为空。
- 五个契约、文件所有权和本波次任务范围无歧义。

#### W1: 数据真实性、执行标签和统计内核（并行，4-7 天）

`C1-DATA / W1-DATA`:

- 完成完整候选池快照、缺失掩码、数据源时间和公告时间持久化。
- 完成 schema 的新库创建与旧库幂等迁移。
- 提供候选批次、执行记录和逐折预测的 repository 接口；此阶段可以先保存空执行/预测记录。
- 对无法证明发布时间的基本面字段标记不可用，不用默认值伪装为已知。
- 测试覆盖新库、旧库升级、重复保存、候选守恒和 point-in-time 拒绝。

`C2-EXECUTION / W1-EXECUTION`:

- 完成版本化 `ExecutionPolicy`，统一次日开盘、涨跌停、停牌、退出与成本语义。
- 输出 `ExecutionRecord`，严格区分 `settled/unfilled/unknown/pending`。
- 删除“数据缺失统一按 -30%”的伪标签；退市后有可实现价格才结算，否则 `unknown` 且禁止晋级。
- 实现低/基准/高成本情景和资金/参与率单调性测试。
- 只调用冻结的 repository 接口，不修改 C1 文件。

`C3-RESEARCH / W1-RESEARCH`:

- 用纯函数和 fixture 实现按交易日配对收益、block bootstrap CI、Holm/FDR 和 purged walk-forward split。
- 统计单位固定为交易日，不把同日多只股票当独立样本。
- 为 1 日与 5 日持有期验证 purge/embargo 无重叠。
- 暂不连接数据库、不训练生产模型、不修改当前排序。

`C0-INTEGRATOR / W1-CONTROL`:

- 统一落地 C1-C3 的配置请求，默认保持复杂模型和 DeepSeek 生产影响关闭。
- 增加契约测试和当前配置/基线只读接口；不接入尚未合并的领域实现。
- 持续审查越界修改和接口漂移。

`G1` 门控与合并顺序:

1. C0 先提交可独立完成的配置/契约改动，确保集成工作树为空。
2. 合并 C1 schema/repository 并运行 C1 测试。
3. 合并 C2 execution/outcome 并运行 C2 测试。
4. 合并 C3 纯统计内核并运行 C3 测试。
5. C0 完成依赖领域实现的最终接线并单独提交。
6. 新旧数据库迁移、成本单调性、样本守恒、purge/embargo 和完整集成测试全部通过。

#### W2: 日级组合基线和端到端 OOS（并行，4-7 天）

`C1-DATA / W2-DATA`:

- 提供按日期/策略/版本读取完整候选、执行记录和基准数据的批量查询。
- 完成历史快照重放和数据质量摘要，禁止 current/legacy baseline 混算。
- 为每日组合与逐折预测提供只追加或版本化持久化接口。

`C2-EXECUTION / W2-PORTFOLIO`:

- 生成冻结规则 Top-5 等权纸面组合、现金和真实权重。
- 接入市场、行业/风格基准及固定种子的 1,000 次候选池随机组合。
- 输出 `DailyPortfolioEvaluation`，包含换手、集中度、容量、未成交率和成本情景。
- 纸面组合与单股结算必须共享同一 `ExecutionPolicy`。

`C3-RESEARCH / W2-OOS`:

- 消费日级组合结果，输出相对冻结基线的每日增量净收益、CI、Sortino、回撤、CVaR 和基准分位。
- 改造 `oos_report.py`，明确 `empty/needs_backfill/insufficient_oos_days/gate_blocked/shadow_eligible`。
- 所有报告显示真实交易日数、baseline ID、成本情景和试验族校正结果。

`C0-INTEGRATOR / W2-PRODUCT`:

- 接入 API 和验证页面，显示实际排序源、行情截止时间、样本数、成本和模型状态。
- 页面不得把 score、概率或 expected return 表述为保证收益。
- 保持旧生产排序不变，任何失败自动回到冻结基线。

`G2` 门控:

- 历史 fixture 可完成“完整候选 -> 排序 -> 组合 -> 成交 -> 日级结果 -> OOS 报告”重放。
- 随机、指数、现金和冻结规则基准均存在，不再显示“未接入基准”。
- C0 运行完整离线测试；网络不可用不能导致测试失败。

#### W3: 样本积累期的审计和简化（并行工程 3-5 天，随后等待真实数据）

`C1-DATA`:

- 建立每日 point-in-time 覆盖率、缺失率、迟到数据、候选数量和待回填状态审计。
- 提供只修数据、不改变历史版本语义的受控回填和完整性校验。

`C2-EXECUTION`:

- 建立组合约束逐步消融框架，但不预先宣称任何约束提高收益。
- 输出执行过滤、成本、容量和未成交分别造成的收益变化。

`C3-RESEARCH`:

- 建立特征 manifest、覆盖率/冗余检查、按日 RankIC、分层收益和 leave-one-feature-out 框架。
- 建立风险重复矩阵，识别同一风险被过滤、扣分、模型和仓位重复处理的情况。
- 前 20 日只做数据诊断；60 日前不生成可晋级模型。

`C0-INTEGRATOR`:

- 从生产路径断开启发式 expected return、独立分桶概率、Meta、Ensemble、手工事件加分和 DeepSeek rerank/veto。
- 可保留 shadow 原始数据采集，但页面不突出无证据字段。
- 增加数据质量和样本成熟度监控。

`G3` 门控:

- 至少 60 个真实、同版本、同 baseline ID 的成熟 OOS 交易日。
- point-in-time 覆盖率不低于 95%，未知标签、未成交和数据源失败均可解释。
- 未达到 G3 时只继续采集和修数据，禁止通过回放样本数量替代真实 OOS 日数。

#### W4: 模型、组合和事件的独立挑战（G3 后并行，5-10 天）

`C1-DATA`:

- 冻结训练/验证数据集、日期范围、schema hash、baseline ID 和数据质量报告。
- 确保测试窗口标签在训练时尚不可见，并保存逐折预测。

`C2-EXECUTION`:

- 按 P5 顺序完成等权、单票 cap、行业 cap、真实相关性、容量、波动率目标和回撤降仓消融。
- 每一步只与上一步和固定仓位比较，不把风险约束包装成 alpha。

`C3-RESEARCH`:

- 只比较冻结规则、等权低维线性模型和一个正则化线性净收益模型。
- 删除启发式 fallback 与手工 `rank_score` 拼分；概率由同一 OOS 收益模型派生。
- 完成特征消融、block bootstrap、Holm/FDR、去极端日和高成本敏感性。
- 只有所有门槛通过才生成 `shadow` artifact；失败结论必须允许“冻结基线胜出”。

`C0-INTEGRATOR`:

- 将 DeepSeek 限定为 point-in-time 结构化事件抽取，保存 evidence hash、prompt/model 版本、token 和失败。
- 组织“基础模型”与“基础模型 + 事件”的相同日期反事实试验，不让 DeepSeek 直接产出最终排名。
- 接入 shadow artifact 展示，但不改变生产排序。

`G4` 门控:

- 模型相对冻结基线的日均增量净收益 95% CI 下界大于 0，且 FDR、风险、成本和稳定性均通过。
- 组合机制逐项有独立消融结论；无贡献项删除或保持关闭。
- DeepSeek/事件未达到 60 个有效事件日时继续 shadow，不参与交易。

#### W5: 灰度、监控和回退（G4 后并行，至少 20 个灰度交易日）

`C1-DATA`:

- 保存灰度分组、实际排序源、artifact hash、执行结果和回退原因，保证完整审计链。

`C2-EXECUTION`:

- 比较 shadow/live 组合的真实成交、成本、容量、未成交和风险差异。
- 验证回退到冻结规则或现金不会遗留旧模型权重。

`C3-RESEARCH`:

- 监控 20 日增量净收益、漂移、覆盖率、校准和回撤；只输出门控结论，不直接改运行开关。

`C0-INTEGRATOR`:

- 实现按日期/账户哈希的 10% -> 30%-50% 确定性灰度和一键回退。
- 负责唯一的生产开关修改、发布检查和前端状态。

`G5` 门控:

- 20 个灰度交易日没有触发回退条件。
- 至少 120 个 OOS 日、两种以上市场状态且正式替换门槛全部通过。
- 任一门槛失败立即回冻结基线；基线健康门控也失败则回现金。

#### W6: 扩展 `swing_picks`

- C1 负责独立的 5 日成熟标签、数据版本和 purge 所需字段。
- C2 负责 2-5 日固定退出、不可卖出和组合成本。
- C3 独立训练/验证，不复制明日模型权重。
- C0 负责策略级开关、API、页面和灰度。
- 正式替换仍需至少 120 个 OOS 交易日；`short_term` 不随本波次升级为可执行策略。

### 6.8 Codex 任务提示词模板

每个写入实例只领取一个波次任务卡。推荐提示词:

```text
你是 <C1-DATA/C2-EXECUTION/C3-RESEARCH>。
共同基线 SHA: <BASE_SHA>；当前任务: <WAVE-TASK-ID>。
先阅读 docs/plan.md 的 2、3、5、6 章和相关现有代码。
只修改 6.2 分配给你的文件；其他文件只读。不要回退或覆盖已有改动。
严格使用 6.5 已冻结契约；如接口不足，停止越界修改，在交接中提出请求。
实现任务卡的最小完整闭环，添加本领域独立测试，先跑定向测试再跑受影响测试。
不要改 config.py、docs、前端或其他领域文件；不要启用任何生产模型开关。
完成后检查 git diff --check，提交一个或多个可独立审查的 commit。
最终按 6.9 模板交接，不自行合并集成分支。
```

`C0` 提示词:

```text
你是 C0-INTEGRATOR。你负责冻结契约、文件租约、配置、API/前端接线、审查、合并和门控。
不得替 C1-C3 修改其领域文件；发现问题退回对应负责人。
每个波次按 C1 -> C2 -> C3 的顺序审查合并，逐次运行门控测试；随后完成 C0 接线提交。
只有 docs/plan.md 中对应 G 门控通过，才发布下一波次任务。
样本不足或收益证据失败时保持冻结基线/现金，不降低门槛。
```

### 6.9 交接、同步和合并协议

每个 Codex 的最终交接必须包含:

```text
Task ID:
Base SHA:
Commit(s):
Owned files changed:
Contract/API used:
Schema migration impact:
Config requests for C0:
Tests run and results:
Unresolved risks:
Next-wave dependency:
```

`docs/codex-progress.md` 使用单一平面表，由 C0 在每次派发、交接、合并和门控后立即更新:

```text
| Task | Wave | Owner | Base SHA | Branch | Status | Commit | Tests | Gate | Blocker |
|---|---|---|---|---|---|---|---|---|---|
| W1-DATA | W1 | C1 | <sha> | codex/c1-data | assigned | | | G1 | |
```

状态只允许 `planned`、`assigned`、`in_progress`、`review`、`merged`、`blocked`、`canceled`。任务进入下一状态前必须有 commit 或明确 blocker；禁止用聊天记录代替账本。

协作规则:

1. 每个 commit 只完成一个任务卡，不夹带格式化或无关重构。
2. Codex 不直接合并其他分支，也不在波次中互相 rebase/merge。
3. C0 在开始合并前必须提交自己的独占文件或暂不修改，禁止带着脏工作树合并。
4. C0 按 C1 -> C2 -> C3 顺序审查并 `git merge --no-ff`，保留任务分支原始 commit；C0 自身改动直接作为独立 commit 留在集成分支，不执行“合并 C0”。
5. 每合并一个领域就运行该领域测试；三个领域合并后，C0 完成依赖接线并运行完整集成测试。
6. 门控通过后，C1-C3 才把新的 integration 分支合入自己的分支，开始下一波次。
7. 合并冲突由文件拥有者解决；C0 不猜测领域语义。
8. 若某文件确需换负责人，C0 先登记临时租约，并确保原负责人停止写入后再转移。

波次结束后的同步示例:

```bash
git -C /home/cp/Public/trader-codex/c1-data merge integration/scientific-ranking
git -C /home/cp/Public/trader-codex/c2-execution merge integration/scientific-ranking
git -C /home/cp/Public/trader-codex/c3-research merge integration/scientific-ranking
```

### 6.10 分层测试和合并门控

各实例从自己的 worktree 根目录运行定向测试；可以复用主工作区的 Python 解释器，但测试路径和导入必须解析到当前 worktree:

```bash
# C1-DATA
/home/cp/Public/trader/.venv/bin/python -m pytest -q \
  tests/scoring/test_validation_store.py \
  tests/scoring/test_validation_repository_runtime.py \
  tests/scoring/test_validation_backfill.py \
  tests/scoring/test_point_in_time_contracts.py \
  tests/scoring/test_validation_schema_contracts.py

# C2-EXECUTION
/home/cp/Public/trader/.venv/bin/python -m pytest -q \
  tests/scoring/test_backtest_exit.py \
  tests/scoring/test_portfolio_risk.py \
  tests/scoring/test_execution_policy_contracts.py \
  tests/scoring/test_daily_portfolio_baseline.py

# C3-RESEARCH
/home/cp/Public/trader/.venv/bin/python -m pytest -q \
  tests/scoring/test_models_calibration.py \
  tests/scoring/test_validation_oos.py \
  tests/scoring/test_purged_walk_forward.py \
  tests/scoring/test_daily_incremental_metrics.py \
  tests/scoring/test_expected_return_artifact.py

# C0-INTEGRATOR
/home/cp/Public/trader/.venv/bin/python -m pytest -q \
  tests/test_frontend_contracts.py \
  tests/test_recommendation_runtime_support.py \
  tests/test_validation_runtime_support.py \
  tests/scoring/test_app_endpoints.py
```

计划中的新增测试文件在对应实现落地时创建；文件尚不存在的早期波次只运行现有测试。每个集成门控至少执行:

```bash
git diff --check
/home/cp/Public/trader/.venv/bin/python -m compileall -q stock_analyzer
/home/cp/Public/trader/.venv/bin/python -m pytest -q
```

此外必须人工/脚本验证:

- 新建数据库和旧版数据库升级都成功且幂等。
- 同一快照重放结果确定，随机基准使用固定种子。
- 网络、DeepSeek 或模型 artifact 失败时，冻结基线不变。
- 当前/旧 baseline、真实/replay、shadow/live 指标不会混算。
- `git status --short` 不包含 `.runtime`、数据库、密钥、缓存或临时交接文件。

### 6.11 并行工期和不可压缩路径

| 阶段 | 4 Codex 工程时间 | 自然时间门槛 | 结束条件 |
|---|---:|---:|---|
| W0 | 1 天 | 无 | 基线、契约、worktree 就绪 |
| W1 | 4-7 天并行 | 无 | G1 数据/标签/统计内核通过 |
| W2 | 4-7 天并行 | 无 | G2 端到端日级组合基线通过 |
| W3 | 3-5 天并行 | 至少 60 个真实 OOS 交易日 | G3 数据与样本就绪 |
| W4 | 5-10 天并行 | 事件试验另需 60 个有效事件日 | G4 仅合格项进入 shadow |
| W5 | 2-4 天工程 + 监控 | 至少 20 个灰度日，正式替换需 120 个 OOS 日 | G5 正式替换或回退 |
| W6 | 复用框架约 5-10 天 | `swing_picks` 至少 120 个 OOS 日 | 波段策略独立门控 |

预计核心工程可在约 3-4 周内完成，但“科学证明有收益贡献”的最早时间由真实交易日决定。任何 Codex 都不得用扩大同日股票数、回放样本或反复调参来伪造时间维度。

### 6.12 多 Codex 完成定义

并行开发完成不等于收益模型可上线。必须分别报告三种状态:

- **Engineering complete**: W0-W3 代码、迁移、测试、重放和监控完成，但生产仍使用冻结基线。
- **Shadow eligible**: G3/G4 通过，模型或事件只产生影子预测，不改变排序和仓位。
- **Production eligible**: G5 通过，才允许 C0 修改唯一生产开关；否则删除无贡献机制或继续冻结。

最终交付应能明确回答: 哪个 Codex 修改了什么、基于哪个 SHA、用了哪个契约、通过了哪些门控，以及该机制是工程完成、shadow 还是具备生产资格。

## 七、验收矩阵

| 维度 | 必须输出 | 阻断条件 |
|---|---|---|
| 数据 | point-in-time 覆盖率、缺失率、延迟、候选池大小 | 覆盖率 <95% 或发现未来数据 |
| 执行 | 成交率、未成交原因、滑点、冲击、容量 | 无法复算或高成本情景失效 |
| 收益 | 每日增量净收益、CI、累计收益、基准分位 | CI 下界 <=0 |
| 风险 | Sortino、最大回撤、CVaR、最差日/月 | 超过预注册容忍度 |
| 稳定 | 各折、月份、行业、市场状态、去极端日 | 单一切片贡献主导 |
| 统计 | 试验数、校正方法、调整后显著性 | 多重检验未通过 |
| 模型 | 特征、训练截止日、漂移、artifact 年龄 | 过期或分布外 |
| 组合 | 权重、现金、集中度、相关性、换手 | cap/容量违规 |
| DeepSeek | 覆盖、误杀、避亏、token/延迟、增量净收益 | 扣成本无正增量 |

## 八、工程任务映射

| 优先级 | 模块 | 任务 |
|---|---|---|
| P0 | `config.py`、运行配置接口 | 固化基线开关、版本与实验登记 |
| P0 | `strategy_validation.py`、`validation_outcomes.py` | 统一可成交标签、成本、未成交和退市状态 |
| P0 | `validation_schema.py`、`validation_repository.py` | 保存完整候选池、特征时间戳、模型/执行版本和逐折预测 |
| P1 | `validation_metrics.py`、`oos_report.py` | 改为日级组合增量、bootstrap CI、基准和成本敏感性 |
| P1 | `paper_trading.py` | 对齐真实组合、现金、成交和指数基准，删除“未接入基准”占位 |
| P1 | `calibrate.py` | 统一 purged walk-forward、试验族校正与消融接口；停止自动坐标调权 |
| P2 | `expected_return_model.py` | 删除启发式伪收益和手工 rank 公式，替换为单一可重建模型 artifact |
| P2 | `probability_calibration.py` | 合并到收益模型 OOS 校准，不再独立影响交易 |
| P2 | `portfolio.py` | 实现按顺序可消融的简单约束和真实历史相关性 |
| P3 | `meta_labeling.py`、`ensemble.py`、`event_alpha.py` | 移除生产接线；保留原始研究数据所需最小代码 |
| P3 | `deepseek/` | 只做结构化 point-in-time 事件抽取、缓存和反事实归因 |
| P3 | 前端与 API | 明确展示实际排序源、数据截至时间、净收益区间、样本数、成本和模型状态 |

## 九、测试计划

### 9.1 单元与属性测试

- 成本增加时净收益不得增加；资金/参与率增加时冲击成本不得下降。
- 任何训练样本的标签成熟时间必须早于测试信号时间。
- purge 后训练/测试标签窗口不得重叠。
- 不可成交、停牌、退市、数据缺失的计数必须守恒。
- 相同快照、版本和随机种子生成相同候选、排序和组合。
- 组合权重非负、总和不超过 1、各类 cap 不被突破，剩余部分为现金。
- artifact 的 `baseline_id`、特征 schema、训练截止日或有效期不匹配时拒绝加载。

### 9.2 集成与回归测试

- 从历史快照端到端重放: 候选池 -> 排序 -> 组合 -> 成交 -> 结果 -> OOS 报告。
- 新旧基线同时运行但指标绝不混算。
- shadow 模型、DeepSeek 超时/失败或缓存损坏不改变基线推荐。
- 灰度分流确定且可审计，回退后下一次请求立即恢复基线。
- 前端不得把 `score`、`p_win`、`expected_return` 或 DeepSeek 观点标成保证收益。

## 十、停止规则与最终取舍

为避免长期维护没有贡献的功能，使用以下停止规则:

- 一项机制累计两个完整 OOS 窗口仍无正增量: 停止试验，移除生产接线和页面主展示。
- 一项因子连续两个 60 日窗口边际贡献为零/负，第三个窗口仍未恢复: 从模型删除。
- DeepSeek/事件特征累计 60 个有效事件日无正增量: 关闭自动调用，仅保留人工按需分析。
- 复杂模型若只提高训练/验证指标、未提高最终 untouched OOS 组合净收益: 删除。
- 新方案若收益略高但换手、容量、回撤或维护复杂度显著恶化: 保留简单基线。
- 当全部策略均未通过健康门控时，正确输出是“无推荐/现金”，不是降低标准凑足股票。

最终系统应只保留四类真正有用的能力: **可交易的候选资格、一个经 OOS 验证的排序、简单透明的组合风控、可审计的真实执行反馈**。其他机制只有在相同数据与成本口径下证明独立边际贡献后，才重新进入生产计划。

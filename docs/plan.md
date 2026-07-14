# 荐股评分机制——聚焦收益提升计划

> 创建时间: 2026-07-14
> 适用模块: `stock_analyzer/` 全部策略、评分、校准、验证模块
> 关联文档: `strategy_and_prediction.md`, `software_design.md`

---

## 零、核心结论

**三种策略（盘中观察/明日优先/2-5日持有）都不能保证收益。**

系统内建了多层保护机制来承认这一点：
- 策略胜率 < 48% 自动退役 (`strategy_health.py`)
- 至少 60 个真实交易日 + 净收益 > 0 + 胜率 > 50% 才放行 (`app_support.py`)
- 7 个高级评分模块全部默认关闭 (`config.py` 中 `ENABLE_* = 0`)
- 系统目标不是"保证收益"，而是"用持续验证闭环管理风险"

但系统已经具备了大量可以**提升收益**的基础设施——大部分代码已完整实现，只是被关闭了。

---

## 一、关键发现：已有但关闭的收益提升模块

经过 37 次代码读取的深度审查，发现系统中有 **7 个高级模块代码完整、但全部被关闭**：

| 模块 | 文件 | 状态 | 关闭方式 | 样本门槛 |
|------|------|------|---------|---------|
| 预期收益排序 | `expected_return_model.py` | 影子模式 | `ENABLE_EXPECTED_RETURN_RANKING=0` | 60真实交易日 |
| 因子交互项 | `calibrate.py` | 影子模式 | `ENABLE_INTERACTION_TERMS=0` | 20样本 |
| Regime权重 | `calibrate.py` | 影子模式 | `ENABLE_REGIME_SPECIFIC_WEIGHTS=0` | 20样本/regime |
| Meta-Labeling | `meta_labeling.py` | 影子模式 | `ENABLE_META_LABELING=0` | 50样本 |
| 概率校准 | `probability_calibration.py` | 诊断模式 | 硬编码`diagnostic_only` | 5分桶×各≥20样本 |
| 事件Alpha | `event_alpha.py` | 研究模式 | 硬编码`research_only` | — |
| Ensemble集成 | `ensemble.py` | 影子模式 | 硬编码`shadow_only` | — |
| Factor IC加权 | `config.py` | 关闭 | `ENABLE_FACTOR_IC_WEIGHTING=0` | 30样本 |

**这意味着提高收益的第一优先级不是写新代码，而是逐步开启已有模块。**

---

## 二、结构性问题（阻塞收益提升的瓶颈）

### 2.1 校准只支持 tomorrow_picks

```python
# calibrate.py:315
if strategy != "tomorrow_picks":
    return {"ok": False, "status": "unsupported_strategy"}
```

**影响**: swing_picks 和 short_term 的权重永远不会被数据驱动优化。swing_picks 使用固定默认权重 (momentum=0.30, trend=0.25, liquidity=0.20, execution=0.15, not_overextended=0.10)，无论市场如何变化。

### 2.2 坐标下降只有 3 档搜索

```python
# calibrate.py:1306
multipliers = (0.7, 1.0, 1.3)
```

5 个权重维度 × 3 档 = 最多 243 种组合。Ridge 回归可以探索连续的参数空间。

### 2.3 所有高级模块是独立影子，没有集成

每个模块独立运行、独立评估，但没有一个统一框架来：
- 自动选择最优模块组合
- 对比不同模块的 OOS 增量贡献
- 在模块失效时自动降级

### 2.4 没有 ML/深度学习

全代码库 0 个神经网络、0 个梯度提升树、0 个随机森林。最接近 ML 的是 KNN 预期收益模型。

---

## 三、聚焦收益提升的行动计划

### 阶段 1：开启已有模块（第 1-4 周）

#### 1.1 开启 Meta-Labeling 信号过滤

**目标**: 用置信度过滤低质量信号，直接提升净胜率

**实现**:
```
步骤 1: 确认验证库 tomorrow_picks 真实样本 ≥ 50
步骤 2: train_meta_label_model("tomorrow_picks", samples)
步骤 3: 在推荐管线中 attach_meta_confidence(rows, model, enforce=True)
步骤 4: 对 confidence < 0.50 的信号降为备选观察 (仓位=0)
步骤 5: 对比开启前后 20 个交易日的 OOS 指标
```

**配置**: `ENABLE_META_LABELING=1`
**预期效果**: 过滤约 20-30% 的低置信信号，净胜率提升 2-5 个百分点
**风险**: 样本不足时模型不拟合，自动回退

#### 1.2 开启预期收益排序（替代原始分数排序）

**目标**: 用历史相似信号的加权平均收益替代线性加权分数

**实现**:
```
步骤 1: 确认验证库 tomorrow_picks 真实交易日 ≥ 60
步骤 2: build_expected_return_artifact("tomorrow_picks", samples)
步骤 3: 运行 evaluate_expected_return_ranking 做 OOS 验证
步骤 4: 如果 OOS 改善 > 0.05，开启 ENABLE_EXPECTED_RETURN_RANKING=1
步骤 5: 预期收益排序的 Top-K 替代原始分数的 Top-K
```

**配置**: `ENABLE_EXPECTED_RETURN_RANKING=1`
**预期效果**: Top-K 选股更精准（KNN 找历史相似信号），平均收益提升 0.1-0.3%
**风险**: 模型过期 (7天自动失效)，需定期重训

#### 1.3 开启概率校准

**目标**: 将原始分数映射为真实概率，辅助 Meta-Labeling 和仓位决策

**实现**:
```
步骤 1: train_score_calibrator("tomorrow_picks", samples)
步骤 2: 在推荐行中附加 calibrated_probability
步骤 3: 修改 probability_role 从 "diagnostic_only" 到 "trading_auxiliary"
步骤 4: 概率 < 50% 的信号降权 (仓位 × 0.5)
```

**配置**: 修改 `probability_calibration.py` 中 `probability_role` 逻辑
**预期效果**: 为 Meta-Labeling 提供第 4 个输入组件，置信度更准确
**风险**: 分桶样本不足时单调性约束可能不满足

#### 1.4 开启因子交互项

**目标**: 捕捉因子间的非线性关系（如高动量+低换手 vs 高动量+高换手的区别）

**实现**:
```
步骤 1: 确认 calibrate_live_weights 有 ≥ 30 个有效样本
步骤 2: ENABLE_INTERACTION_TERMS=1
步骤 3: _fit_interaction_terms 自动筛选 8 对最优交互项
步骤 4: OOS walk-forward 验证，改善 > 0.05 才合入
```

**配置**: `ENABLE_INTERACTION_TERMS=1`
**预期效果**: 捕捉量价因子的非线性关系，Sharpe 提升 5-10%
**风险**: 过拟合（已用 L2 正则化 + walk-forward OOS 保护）

#### 1.5 开启 Regime-Specific 权重

**目标**: risk_on/balanced/risk_off 三种市况下使用不同权重

**实现**:
```
步骤 1: 确认各 regime 下样本 ≥ 20
步骤 2: ENABLE_REGIME_SPECIFIC_WEIGHTS=1
步骤 3: _fit_regime_specific_weights 分别拟合三种权重
步骤 4: 每个 regime 独立 OOS 验证
```

**配置**: `ENABLE_REGIME_SPECIFIC_WEIGHTS=1`
**预期效果**: risk_off 时弱化动量、强化流动性，减少回撤
**风险**: 样本不足时回退 balanced 权重

---

### 阶段 2：扩展校准覆盖面（第 3-6 周）

#### 2.1 为 swing_picks 添加 live_weights 校准

**目标**: swing_picks 的 5 个维度权重也能被数据驱动优化

**实现**:
```
步骤 1: 在 calibrate.py 中移除 "only tomorrow_picks" 限制
步骤 2: 为 swing_picks 实现 _evaluate_live_samples 适配（使用 signal_exit_return 而非 signal_next_close_return）
步骤 3: 为 swing_picks 实现 _walk_forward_evaluate（使用 swing 专属的退出规则回测）
步骤 4: 独立 OOS 验证
```

**关键改动**: `calibrate.py:315-316` 移除 `if strategy != "tomorrow_picks"` 限制，增加 swing 分支的 evaluate 逻辑。

#### 2.2 校准搜索从 3 档扩展到连续空间

**目标**: 用 Ridge 回归替代坐标下降的 3 档搜索

**实现**:
```
步骤 1: 实现 _fit_weights_ridge(strategy, samples, alpha=1.0)
步骤 2: 特征矩阵 X = [liquidity_score, momentum_score, historical_edge_score, execution_score, tail_setup_score]
步骤 3: 目标 y = primary_return_net (实际净收益)
步骤 4: Ridge 回归得到连续权重
步骤 5: 与 3 档坐标下降做 OOS 对比，改善 > 0.05 才切换
```

**预期效果**: 探索连续参数空间，找到比 243 种组合更优的权重

---

### 阶段 3：新逻辑引入（第 5-8 周）

#### 3.1 集成层上线

**目标**: 将多个独立模块融合为一个集成评分

**实现**:
```
步骤 1: 修改 ensemble.py 中 hardcoded shadow_only → 由 ENABLE_ENSEMBLE 控制
步骤 2: 配置 ENSEMBLE_MODEL_WEIGHTS（初始: price_volume=0.40, expected_return=0.25, probability=0.15, meta=0.10, event=0.10）
步骤 3: 在推荐管线中 attach_ensemble_score(rows, enforce=True)
步骤 4: 用集成评分替代单一原始分数排序
步骤 5: OOS 验证集成评分 vs 原始评分
```

**预期效果**: 不相关 alpha 源叠加提升 Sharpe

#### 3.2 组合层优化

**目标**: 在已有选股结果上做 Max-Sharpe 组合权重分配

**实现**:
```
步骤 1: 在 portfolio.py 中增加 _max_sharpe_weights() 函数
步骤 2: 输入: 候选股预期收益 + 协方差矩阵(近60日)
步骤 3: 约束: 单票 ≤ 25%, 主题 ≤ 35%, 仅做多
步骤 4: 与当前等权+主题约束投影做 OOS 对比
```

**预期效果**: 分散个股相关性风险，同等胜率下回撤更小

---

### 阶段 4：ML 增强（第 6-12 周，可选）

#### 4.1 LightGBM 排名模型

**目标**: 用梯度提升树学习非线性因子组合

**实现**:
```
步骤 1: 从验证库提取历史信号的特征和收益
步骤 2: 特征: 所有评分维度 + 原始量价因子 + 盘面状态特征
步骤 3: 标签: 次日/2-5日净收益
步骤 4: LightGBM ranker (objective="lambdarank")
步骤 5: 时间序列 walk-forward 训练和验证
步骤 6: OOS 对比 vs 当前线性模型
```

**风险**: 过拟合风险高，需要严格的时间序列交叉验证和足够样本量

---

## 四、实施优先级矩阵

```
                  高收益
                    │
    1.1 Meta-Labeling    1.2 预期收益排序
    1.3 概率校准         1.4 交互项
    2.1 swing校准        1.5 Regime权重
                    │
  低投入 ─────────────┼───────────── 高投入
                    │
    2.2 Ridge替代        3.1 集成层上线
    坐标下降             3.2 组合层优化
                    │
                        4.1 LightGBM排名
                    │
                  低收益 (相对)
```

**推荐顺序**:
1. **第 1 周**: 1.1 Meta-Labeling + 1.3 概率校准（代码完整，投入最低）
2. **第 2 周**: 1.2 预期收益排序（需 60 天样本，如果不够则跳过）
3. **第 3-4 周**: 1.4 交互项 + 1.5 Regime 权重
4. **第 4-5 周**: 2.1 swing 校准
5. **第 5-6 周**: 2.2 Ridge 替代坐标下降
6. **第 7-8 周**: 3.1 集成 + 3.2 组合优化
7. **第 9-12 周**: 4.1 LightGBM（如前述改进已显著改善则观察，不急于上线）

---

## 五、每个阶段的验证标准

每个模块开启前必须通过以下门控：

| 门控 | 标准 |
|------|------|
| 样本量 | 满足模块最小样本要求 |
| OOS 改善 | walk-forward 4 折 OOS objective 改善 > 0.05 |
| 正面折数 | 4 折中 ≥ 3 折正面 |
| FDR 控制 | BH-FDR q=0.1 下显著 |
| 回撤约束 | 开启后平均回撤不差于开启前 |
| 灰度发布 | 先影子运行 5 个交易日，确认无异常再开启 |

---

## 六、配置开关总览

```python
# 阶段 1：开启已有模块
ENABLE_META_LABELING = "1"           # 1.1 置信度过滤
ENABLE_EXPECTED_RETURN_RANKING = "1" # 1.2 预期收益排序（需 ≥60 天样本）
ENABLE_INTERACTION_TERMS = "1"       # 1.4 因子交互项
ENABLE_REGIME_SPECIFIC_WEIGHTS = "1" # 1.5 分市场状态权重
ENABLE_FACTOR_IC_WEIGHTING = "1"     # Factor IC 动态加权

# 阶段 2-3：新逻辑
ENABLE_SWING_CALIBRATION = "1"       # 2.1 swing 校准（新开关）
ENABLE_RIDGE_CALIBRATION = "1"       # 2.2 Ridge 回归（新开关）
ENABLE_ENSEMBLE = "1"                # 3.1 集成评分
ENABLE_PORTFOLIO_OPTIMIZATION = "1"  # 3.2 组合优化（新开关）

# 阶段 4：ML
ENABLE_LIGHTGBM_RANKING = "1"        # 4.1 LightGBM（新开关，需额外依赖）
```

全部设为 `"0"` 即可恢复到当前行为。

---

## 七、风险与缓解

| 风险 | 级别 | 缓解措施 |
|------|------|---------|
| Meta-Labeling 样本不足 | 低 | 自动回退，`is_fitted=False` 时不执行过滤 |
| 预期收益模型过期 | 低 | 7天自动失效，过期回退原始分数排序 |
| 交互项过拟合 | 中 | L2 正则化 + walk-forward OOS + FDR 三重保护 |
| Regime 权重样本不足 | 中 | 单 regime < 20 样本时回退 balanced 权重 |
| swing 校准数据量不足 | 中 | swing 信号频率低于 tomorrow，需更长积累期 |
| LightGBM 过拟合 | 高 | 严格时间序列 CV + 样本量 ≥ 200 + OOS 验证 |
| 多个模块同时开启互相干扰 | 中 | 逐模块灰度，每次只开一个，隔离评估 |

---

## 八、核心原则

1. **"提高收益" = 提升风险调整后期望收益，不是保证正收益**
2. **优先开启已有代码，其次扩展覆盖面，最后引入新逻辑**
3. **每个模块必须独立 OOS 验证通过才合入**
4. **全部有独立开关，任一步骤可回退**
5. **影子模式先行，灰度发布，隔离评估**

真正的 alpha 来源于：
- **更多不相关的 alpha 源** (预期收益排序、事件 alpha、集成)
- **更好的风险控制** (Meta-Labeling 过滤、Regime 权重、组合优化)
- **更长的策略有效期** (数据驱动校准、FDR 门控、时间衰减)

---

## 九、本轮独立 Review 对比与新增收益杠杆（2026-07-14 复核）

> 本章是"重新 review + 与本文档对比"的产物。结论：**本文档（阶段 1-4）方向正确，但遗漏了三个比"开启 7 模块"投入产出比更高、且已具备代码基础的收益杠杆，并且对现状有 3 处失准。**

### 9.1 本文档已覆盖（确认无误）

- 7 个高级模块（Meta-Labeling / 预期收益排序 / 交互项 / Regime 权重 / 概率校准 / 事件 Alpha / Ensemble）代码已存在、默认关闭，应逐步开启 ✅
- 校准仅支持 `tomorrow_picks`（`calibrate.py` 硬编码 `if strategy != "tomorrow_picks": return unsupported_strategy`）✅
- 坐标下降仅 3 档 `{0.7,1.0,1.3}` ✅
- 无 ML/深度学习 ✅

### 9.2 本文档遗漏 / 失准的 6 点

#### 9.2.1 【最高优先级，遗漏】退出/止损逻辑是写死的固定值，且从未被优化

**代码证据**：
- `execution_policy.py:21-24` — `take_profit_pct=8.0 / stop_loss_pct=5.0 / trailing_stop_pct=4.0`（tomorrow 与 swing 完全相同）
- `risk_rules.py:14-20` `default_exit_policy()` — 上述值来自 `EXIT_TAKE_PROFIT_PCT / EXIT_STOP_LOSS_PCT / EXIT_TRAILING_STOP_PCT`，**默认 8/5/4，且对所有策略、所有市况一视同仁**
- `backtest.py:67,162` — `simulate_exit()` 在回测中真实调用，即退出规则**直接决定回测与实盘盈亏**

**为什么这是最大杠杆**：
- 对 `swing_picks`（5 日持有），8/5/4 的止盈止损/移动止损是**盈亏的主导因素**，选股再准，退出不当也会把盈利变亏损或把小亏变大亏。
- 对 `tomorrow_picks`（1 日持有），固定 8% 止盈在次日极少触发、5% 止损在波动票上频繁触发——等于"截断利润、放任亏损"，与期望相反。
- 这些值**从未用历史数据校准**，也未随 regime 调整。

**待办**：见 9.3。

#### 9.2.2 【高优先级，遗漏】"数据/执行层诚实化"开关已实现但默认关闭，本文档完全删除了这一层

**代码证据**（与上一版 plan 的 6.5 节对应，但本版 plan 已删除，且这些功能其实已经写好了，只是 OFF）：
- `execution_policy.py:91` `ENABLE_TAIL_AUCTION_SLIPPAGE`（尾盘集合竞价滑点，默认 False）
- `execution_policy.py:97` + `portfolio.py:131` `ENABLE_MARKET_IMPACT`（平方根市场冲击，默认 False）
- `portfolio.py:264` `ENABLE_REGIME_THEME_CAP`（主题上限随市况，默认 False）
- `strategy_validation.py:394` `ENABLE_SURVIVORSHIP_CORRECTION`（退市/幸存者修正，默认 False）

**为什么重要**：本文档阶段 1 要在"未开启这些开关"的回测上验证并开启 Meta-Labeling/Ensemble。如果回测本身高估了收益（没算尾盘滑点、没算市场冲击、没修正退市），那么在**被高估的指标**上开启模块，可能选出"虚假有效"的组合——这是比过拟合更隐蔽的陷阱。

**待办**：把"开启这 4 个已实现的诚实化开关"作为 **步骤 0**，先于所有模块开启（见 9.4）。

#### 9.2.3 【中，失准】组合优化并非"缺失"，本文档 3.2 描述与现状不符

**代码证据**：`portfolio.py:227-236` `_raw_weight = confidence / risk / vol_penalty * expected_edge_multiplier`，并已有主题上限、相关性上限、regime 总仓、回撤降仓、波动率目标化（`_gross_exposure` / `_volatility_target_factor`）。`ENABLE_PORTFOLIO_OPTIMIZATION` 默认即 True。

**结论**：当前不是"等权+主题约束投影"，而是**置信度/风险/波动率加权 + 多重约束**的成熟分仓。本文档 3.2 提出"Max-Sharpe 组合优化"是**冗余且误述现状**。更值得做的不是替换，而是：
- 用近 60 日协方差做**相关性降权**（已有 `correlation_cap` 但用的是行业/主题代理，非真实收益协方差）；
- 把 `expected_edge_multiplier` 的权重从启发式（`1+expected/8`）升级为 OOS 校准。

#### 9.2.4 【中，遗漏】因子丰富度缺口——`tomorrow_picks` 只用量价，无任何基本面/资金流/舆情

**代码证据**：`tomorrow_score.py` 的 5 个组件（liquidity/momentum/historical_edge/execution/tail_setup）全部由 `pct_chg/speed/volume_ratio/turnover/ret_5_10_20d/ma20_gap/volatility/breakout` 构成——纯量价。
- 无**相对强度 vs 行业/指数**（只有绝对涨幅分位）
- 无**资金流/主力净流入**
- 无**基本面质量/估值**（`swing` 也仅通过 regime 间接用到 quality，tomorrow 完全没用）
- 无**舆情/新闻**（`short_term` 用了 sentiment，tomorrow 没用）

**为什么是 alpha 缺口**：纯量价在 A 股极易拥挤、反转。加入不相关的质量/资金流/舆情因子，是突破当前天花板比"调权重"更根本的路径。本文档的"事件 Alpha"模块（`event_alpha.py`，硬编码 `research_only`）正是此类，但被束之高阁。

#### 9.2.5 【低，失准】`盘中观察(short_term)` 不是可执行策略，本文档当作平行策略对待

**代码证据**：`execution_policy.py:17` `holding_days=0` 且 `executable=False`，退出策略全 0，`primary_timing="same_trade_day_close_observation"`；`recommendation_policy.py:11` `apply_today_next_day_gate` 要求 short_term 标的**必须同时出现在明日策略**才保留。

**结论**：`盘中观察` 是**零仓位观察池**，不产生独立买入指令，其价值是"为明日策略提供候选 + 盘中异动预警"。优化方向应是"如何提高它对明日策略的候选质量"，而非"如何让盘中观察自身收益更高"。本文档应明确这一边界，避免把精力投错地方。

#### 9.2.6 【低，失准】配置开关格式与函数名与代码不符

- 本文档把开关写成字符串 `ENABLE_META_LABELING = "1"`，但代码实际是**布尔属性**：`bool(getattr(config, "ENABLE_META_LABELING", False))`（见 `calibrate.py:768`、`recommendation_runtime_support.py:145`），测试中也断言 `config.ENABLE_EXPECTED_RETURN_RANKING` 为布尔。应改为布尔或 `os.getenv(...)=="1"` 形式。
- 本文档写"在管线中 `attach_meta_confidence`"，实际函数名为 `attach_meta_labeling`（`app_support.py:486`）；集成函数为 `attach_ensemble_score`（`ensemble.py:40`）。应照代码实际命名。

### 9.3 退出逻辑优化（新增最高优先级杠杆）

**目标**：用历史数据校准 per-strategy / per-regime 的止盈止损/移动止损，而非全局写死 8/5/4。

**实现**：
```
步骤 1: 从 validation 库抽取 tomorrow_picks / swing_picks 的 (entry, future_kline) 配对
步骤 2: 对每个候选参数网格做 walk-forward：
        TP ∈ {4,6,8,10,12}%, SL ∈ {3,4,5,6,7}%, Trail ∈ {0,2,3,4,5}%
        并按 regime(risk_on/balanced/risk_off) 分别拟合
步骤 3: 以 OOS 期望收益 / 收益回撤比为目标，选最优 (TP,SL,Trail)
步骤 4: 写入 config: EXIT_TAKE_PROFIT_PCT / EXIT_STOP_LOSS_PCT / EXIT_TRAILING_STOP_PCT
        （可加 EXIT_TAKE_PROFIT_PCT_RISK_ON 等 regime 变体）
步骤 5: 灰度 5 个交易日，对比开启前后 OOS 指标
```

**额外增强（可选，更高阶）**：
- **分批止盈**：达标 +X% 先平 1/2，剩余用移动止损跟随，提升盈利交易的期望收益；
- **时间止损**：swing 持仓超过 N 日未触发任何退出则强制平仓，避免"横盘耗时间"；
- **基于波动率的动态止损**：`SL = k * volatility_20d`，高波动票放宽止损避免被洗，低波动票收紧。

**预期效果**：同等选股下，swing 期望收益提升 0.3-1.0%/笔，tomorrow 减少"截断利润"损耗。
**风险**：参数过拟合 → 用 walk-forward + FDR 门控；regime 误判 → regime 变体独立验证。

### 9.4 修订后的优先级顺序（取代本文档第四节的推荐顺序）

```
步骤 0 (前置, 必做, 投入极低): 开启 4 个已实现的"诚实化"开关
    ├─ ENABLE_SURVIVORSHIP_CORRECTION
    ├─ ENABLE_TAIL_AUCTION_SLIPPAGE
    ├─ ENABLE_MARKET_IMPACT
    └─ ENABLE_REGIME_THEME_CAP
    ↓ 重新基线化所有策略的真实 metrics

步骤 1 (最高杠杆, 1-2 周): 退出逻辑校准 (9.3)
    ↓ 同等选股下直接抬升盈亏比

步骤 2 (1-4 周): 开启 7 模块 (本文档阶段 1)
    ├─ 1.1 Meta-Labeling + 1.3 概率校准
    ├─ 1.2 预期收益排序 (≥60 天样本)
    ├─ 1.4 交互项 + 1.5 Regime 权重 + Factor IC 加权

步骤 3 (3-6 周): 扩展校准 + 因子丰富度 (本文档 2.1/2.2 + 9.2.4)
    ├─ swing 校准、Ridge 替代 3 档
    └─ 为 tomorrow 引入 质量/资金流/舆情 因子

步骤 4 (5-8 周): 集成 + 组合层微调 (本文档 3.1 + 9.2.3 修正版)
    ├─ Ensemble 集成
    └─ 协方差相关性降权 + expected_edge 校准 (非 Max-Sharpe 替换)

步骤 5 (9-12 周, 可选): ML 排名 (本文档 4.1)
```

### 9.5 对本文档配置开关的修正

```python
# 步骤 0：开启已实现但默认关闭的"诚实化"开关（非新增代码，仅改默认值）
ENABLE_SURVIVORSHIP_CORRECTION = True
ENABLE_TAIL_AUCTION_SLIPPAGE = True
ENABLE_MARKET_IMPACT = True
ENABLE_REGIME_THEME_CAP = True

# 步骤 1：退出逻辑校准结果（示例，需 walk-forward 得出）
EXIT_TAKE_PROFIT_PCT = 10.0          # swing 可更高
EXIT_STOP_LOSS_PCT = 4.0
EXIT_TRAILING_STOP_PCT = 3.0

# 阶段 1：开启已有模块（布尔开关，非字符串 "1"）
ENABLE_META_LABELING = True
ENABLE_EXPECTED_RETURN_RANKING = True
ENABLE_INTERACTION_TERMS = True
ENABLE_REGIME_SPECIFIC_WEIGHTS = True
ENABLE_FACTOR_IC_WEIGHTING = True

# 阶段 2-3
ENABLE_SWING_CALIBRATION = True
ENABLE_RIDGE_CALIBRATION = True
ENABLE_ENSEMBLE = True
# 注意：组合优化已默认开启，无需新开关；只需校准 expected_edge 系数
```

> 全部恢复为 `False`/注释即可回到当前行为。

---

## 十、第三轮复核：已流转但未接线的 alpha 因子（2026-07-14 三审）

> 本章是又一次"重新 review"的产物。结论：**在退出逻辑(9.3)之外，还存在一个投入产出比更高、且数据已就绪的杠杆——把已经流入管线、甚至已经计算、却从未参与评分组合的 alpha 因子接进去。**

### 10.1 复核正面结论：PIT 纪律已扎实，路径可行

`point_in_time.py` 主动校验：行情/基本面/公告/事件/alphalite 因子的观测时间不得晚于信号截止，并产出 `point_in_time_violations`（含 `future_announcement`/`future_quote_observed_at`/`signal_after_recommendation_cutoff` 等）。**look-ahead 层是诚实的**。因此第九章 9.2.2 的 4 个诚实化开关（幸存者/尾盘滑点/市场冲击/regime 主题上限）补齐后，验证基础即可信，"开模块"路径成立。

### 10.2 新发现 #1【最高 ROI】：已流转但未接线的 alpha 因子

**代码证据**：

| 因子 | 状态 | 证据 |
|------|------|------|
| `close_vs_vwap` / `upper_wick_ratio` / `lower_wick_ratio` / `price_position_20d` / `consecutive_up_days` / `amplitude_5d_mean` | **已计算但从未被评分或回测消费** | `factors.py:102` 受 `ENABLE_ENHANCED_FACTORS`(默认 False) 控制；全仓搜索这些字段仅出现在 `factors.py`/`normalization.py`/`deepseek/feature_schema.py`，**scoring_core 任何 score 函数都不引用**；`backtest._alphalite_signal` 权重字典（`backtest.py:14-22`）也不含它们 |
| `main_net_flow_1d`(主力净流入) / `order_imbalance`(委比) | **管线接收但未打分** | `normalization.py:48-50` 在 `COLUMN_ALIASES` 中，行情可入库；但无任何 score 函数引用 |
| `fundamental_quality_score` / `fundamental_value_score` / `earnings_surprise_score` / `rating_revision_score` | **PIT 已跟踪、组件表已登记，但不在任何策略组合项里** | `point_in_time.py:55-60` 列为 `_FUNDAMENTAL_DERIVED_FIELDS` 并跟踪；`weights.py:111-116` `COMPONENT_FACTOR_KEYS` 已登记映射；但 `STRATEGY_COMBINERS`（`weights.py:72-103`）三个策略的 `terms` 里**均无这些组件** → 基本面只作风险层，从不作正向 alpha |

**为什么这是最高 ROI**：
- 数据**已经在管线里流动**（行情字段、alphalite 因子、基本面都已被 attach），PIT 覆盖也已就位——只差"在 score 函数里加一个 term"。
- 相比写新数据源/新模型，这是**纯接线**，成本最低、风险最小、可逐个开关灰度。
- 这些因子与现有量价因子**低相关**（资金流方向、K 线形态、基本面质量），正是突破纯量价天花板所需的不相关 alpha 源。

**待办（接线计划）**：
```
步骤 1: 打开 ENABLE_ENHANCED_FACTORS=1，先确认计算无误（已有 PIT 覆盖）
步骤 2: 在 STRATEGY_COMBINERS.tomorrow_picks 增加：
        - tail_setup 用 price_position_20d / close_vs_vwap 增强（尾盘价位质量）
        - 新增 wick_quality term（上影线短=承接强）
步骤 3: 为 tomorrow/short_term 增加 main_net_flow term（主力净流入分位）
        （需确认实时行情提供该字段，否则仅在回测可用）
步骤 4: 在 STRATEGY_COMBINERS.swing_picks 增加 fundamental_quality term
        （swing 持有期长，基本面质量有效）
步骤 5: 每接入一个因子做 walk-forward OOS：改善>0.05 且 4 折≥3 正面才保留；
        用 Factor IC（已有 ENABLE_FACTOR_IC_WEIGHTING）动态加权/降权失效因子
步骤 6: FDR 门控（避免接线过多导致假发现）
```

**预期效果**：tomorrow 因子从纯量价扩到"量价+形态+资金流"，swing 扩到"+基本面质量"，Sharpe 提升 5-15%。
**风险**：实时行情可能不提供 main_net_flow → 该因子仅回测可用，需 feature-available 降级；过拟合 → 单因子灰度 + FDR。

### 10.3 新发现 #2【中】：regime 检测是未校准的启发式公式

**代码证据**：`market_regime.py:58-63`
```
score = 50 + median_pct_chg*7.5 + (breadth_pct-50)*0.55 + (strong_pct-weak_pct)*0.35 - max(0,avg_amplitude-7)*2.4
```
- 系数全是手填启发式，从未用历史数据校准或学习。
- regime 一旦误判，会**级联**影响：regime_bonus、`_regime_weight`（权重）、`ENABLE_REGIME_THEME_CAP`（主题上限）、`_gross_exposure`（总仓）。
- 当前 regime 仅用**当日盘中快照**，无趋势/历史动量，易在转折点误判。

**待办**：用历史"次日是否普涨"作标签，对 regime 评分公式做 logistic/分位回归校准；或至少加入"近 5 日 breadth 趋势"减少转折点抖动。优先级低于 10.2（接线因子），因为 regime 误判是二阶影响。

### 10.4 新发现 #3【修正 9.2】：校准目标函数已含 Sortino/尾部/时间衰减

第九章把校准目标描述为"命中率+平均收益"。复核 `calibrate.py:93-119` `_objective()`：tomorrow_picks 实际用 `absolute_win_rate + avg_return*2 + 尾部分位*1.6 + 平均回撤*0.25 + Sortino*0.4 + 最大回撤*0.5`，再乘 `_time_decay_multiplier`。**目标函数已相当成熟**，主要瓶颈仍是 ①3 档网格 ②仅 tomorrow。第九章 2.2（Ridge 替代）与 2.1（swing 校准）依然是正确方向，但"目标函数粗糙"的暗示应撤回。

### 10.5 三种策略的针对性结论

| 策略 | 本质 | 收益主导因素 | 最高 ROI 改进 |
|------|------|------------|--------------|
| 盘中观察(`short_term`) | 非可执行观察池，须与明日共识 | 不直接产生盈亏 | 提高对明日策略的**候选质量**（接线资金流/形态因子），而非追求自身收益 |
| 明日优先(`tomorrow_picks`) | T 日 14:30 入场、T+1 退出 | 入场 setup + 次日缺口 + 固定退出 | ①退出校准(9.3) ②接线形态/资金流因子(10.2) ③Meta-Labeling 过滤 |
| 2-5日(`swing_picks`) | 5 日持有、8/5/4 退出 | **退出规则** + 趋势/动量 | ①退出校准(9.3，最高) ②接线基本面质量(10.2) ③swing 校准(2.1) |

### 10.6 修订后的最终优先级（取代 9.4）

```
步骤0(必做,极低投入): 开启4个已实现诚实化开关 (9.2.2) → 重新基线化
步骤1(最高ROI-接线,1-2周): 接入已流转但未接线的alpha因子 (10.2)
        ├─ ENABLE_ENHANCED_FACTORS + tomorrow 接 price_position/wick/close_vs_vwap
        ├─ tomorrow/short_term 接 main_net_flow（若实时可用）
        └─ swing 接 fundamental_quality
步骤2(最高ROI-退出,1-2周): 退出逻辑 per-strategy/regime 校准 (9.3)
步骤3(1-4周): 开启7模块 (阶段1: Meta-Labeling/预期收益/交互项/Regime/Factor-IC)
步骤4(3-6周): swing校准 + Ridge 替代3档 (2.1/2.2)
步骤5(5-8周): Ensemble + 协方差相关性降权 (3.1 + 9.2.3修正)
步骤6(中,可穿插): regime 检测校准 (10.3)
步骤7(9-12周,可选): ML排名 (4.1)
```

**核心判断**：本轮复核后，**最高 ROI 不再是"开 7 模块"，而是"接线已流转的 alpha 因子(10.2) + 退出校准(9.3)"**——二者都建立在已就绪的数据/代码之上，成本远低于新写模型，且能直接抬升三种策略的风险调整后收益。开 7 模块应排在这两步之后。

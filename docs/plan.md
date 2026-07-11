# 荐股策略与工程优化计划

> 创建时间: 2026-07-10
> 适用模块: `stock_analyzer/scoring.py`, `stock_analyzer/calibrate.py`, `stock_analyzer/factors.py`, `stock_analyzer/strategy_validation.py`, `stock_analyzer/portfolio.py`, `stock_analyzer/app_support.py`
> 关联文档: `strategy_and_prediction.md`, `software_design.md`

本文档的目标是把现有"0-100 综合排序分"逐步升级为"扣成本后的期望收益 / 下行风险 / 置信度"共同驱动的工程闭环。所有改进只追求提升风险调整后收益与验证可信度, 不承诺也不暗示保证盈利。

---

## 一、当前机制总览与局限分析

### 1.1 当前评分公式

```
score = Σ(factor_score_i × weight_i) - risk_penalty + regime_bonus
        × overheat_damp
```

三个策略的维度权重如下：

| 策略 | 维度及权重 |
|------|-----------|
| 明日优先 | liquidity(30%) + momentum(20%) + historical_edge(20%) + execution(15%) + tail_setup(15%) |
| 2-5日持有 | momentum(30%) + trend(25%) + liquidity(20%) + execution(15%) + not_overextended(10%) |
| 盘中观察 | momentum(35%) + liquidity(25%) + sentiment(20%) + risk_guard(10%) + industry(10%) |

### 1.2 校准方式

- **坐标下降**: 对每个权重在 {0.7x, 1.0x, 1.3x} 三档扫描
- **Walk-forward 交叉验证**: 4 折时序分割, 仅 OOS 改善超过 0.05 且多数折正面时生效
- **目标函数**: `win_rate × 3.0 + avg_return × 0.6 + median_return × 0.2 + open_gap × 0.2 + downside_penalty`

### 1.3 核心局限

| # | 局限 | 影响 | 严重度 |
|---|------|------|--------|
| 1 | **线性独立假设**: 因子之间不存在交互 | 高动量+高换手 vs 高动量+低换手被等价对待 | 🔴 高 |
| 2 | **硬阈值规则**: `amplitude >= 11 → penalty=10` | 10.9% 和 11.1% 差异仅 0.2% 却触发 10 分惩罚 | 🔴 高 |
| 3 | **粗粒度校准**: 只搜索 3 个乘数点 | 大量权重组合从未被探索 | 🟡 中 |
| 4 | **均等样本权重**: 无时间衰减 | 120 天前的样本与昨天同等重要, 市场结构变化被忽略 | 🟡 中 |
| 5 | **目标函数单一**: 未计入尾部风险/收益分布偏度 | 可能偏好"高胜率小赚+偶尔大亏"的策略 | 🟡 中 |
| 6 | **分数不可解释为概率**: "75分"只代表排序 | 用户无法直观理解实际胜率含义 | 🟡 中 |
| 7 | **单一面板模型**: 所有盘面环境共用一套权重 | risk_off 时应弱化动量、强化流动性 | 🟢 低 |
| 8 | **因子池有限**: 仅 14 个日线 AlphaLite 因子 | 缺少日内结构、板块相对强度、市场微观结构特征 | 🟢 低 |
| 9 | **排序目标不是预期净收益**: 最终 `score` 仍主要表示强弱/质量 | 高分股票未必对应更高的扣成本期望收益, 也无法直接进入组合优化 | 🔴 高 |

### 1.4 独立 Review 的补充发现（代码层面的深层问题）

> 以下为对三个策略源码逐条 review 后, 在 1.3 之外新发现的结构性问题。

#### 1.4.1 「不能保证收益」是系统内建事实, 而非免责话术

- **退场机制即铁证** (`strategy_health.py:57`): 胜率 < 48% 或 平均净收益 ≤ 0 或 回撤 ≤ -8% → 自动 `retired`。这个机制的存在本身证明**策略必然存在失效期**, 否则无需保护。
- **多处硬声明**: `scoring.py:1270`「综合分不等于上涨概率, 也不代表保证收益」; `README.md:5`「不保证盈利」; `strategy_and_prediction.md:5`「不能保证未来收益」。
- **本质**: 打分是横截面排序 (`prediction_type = "rank_score"`), 不是概率预测, 更不是收益保证。任何改进都不改变这一根本属性。

#### 1.4.2 回测与生产策略明确脱钩, 现有回测结果不可外推

- `backtest.py:1511` 标注:「独立 AlphaLite 研究回测, 不代表明日优先或2-5日生产策略收益」; `/api/backtest` 返回 `production_strategy_validation = false`。
- **影响**: 现有回测无法用于"证明策略能赚钱", 只能用于因子研究。真正的收益证据只能来自验证库的真实前瞻样本 (需 ≥ 20 个真实交易日)。
- **待办**: 需建立一条与生产策略口径一致的回测链路 (见 2.2.1 / 2.3 的 walk-forward OOS 验证), 才能对改进做可信评估。

#### 1.4.3 明日优先的盘中可靠性天然低于尾盘

- 14:30 前的盘中模式下, `tail_setup_score` 固定为 50.0 (`scoring.py:1057`), 占 15% 权重的尾盘结构维度**完全失效**。
- 盘中风险惩罚也走 `provisional=True` 分支, 跳过收盘位置相关的多项扣分。
- **影响**: 盘中推荐本质是"未完成信号", 可靠性低于尾盘确认后的推荐, 但当前两者共用同一套阈值和展示口径, 用户难以区分成熟度。
- **待办**: 应对盘中/尾盘分别校准阈值, 或在盘中显式标注置信度折价 (可结合 2.2.2 概率校准)。

#### 1.4.4 盘中强势观察并非荐股策略

- `recommendation_class = "intraday_observation"`, `trade_action.position_size = 0`, `execution_allowed = False`。
- **它不形成买入指令**, 谈"保证收益"没有意义; 其价值在于为明日优先/2-5日提供横截面强度参考。
- **待办**: 若要让它产生 alpha, 需将其升级为可执行策略并纳入独立验证 (风险较高, 优先级低)。

#### 1.4.5 验证门控是二元的, 缺乏渐进降权

- 现状 (`app_support.py`): 要么"执行", 要么"全退降为备选观察", 中间没有连续档位。
- **影响**: 策略从"边缘可用"到"完全禁用"是断崖式切换, 无法平滑管理边缘表现期。
- **待办**: 由 2.3.3「动态仓位缩放」补齐此缺口 (连续仓位因子替代二元开关)。

#### 1.4.6 本轮补充: 打分优化的突出方向是"收益目标化"

plan.md 原有优化重点已经覆盖了平滑阈值、Sortino、时间衰减、交互项、概率校准、Meta-Labeling、组合优化和压力测试。这些内容解决的是"评分更稳、更不容易过拟合、更能解释风险"。

本轮需要合并的突出点是: **最终排序目标应从 `0-100 综合强弱分` 升级为 `扣成本后的期望收益分`**。也就是说:

```
旧目标: score = 综合强弱排序
新目标: rank_score = E(primary_return_net) - downside_risk - execution_cost - uncertainty_penalty
```

对应输出字段应从单一 `score` 扩展为:

| 字段 | 含义 | 用途 |
|------|------|------|
| `expected_return_net` | 预测扣成本主周期净收益 | 排序和组合优化的核心输入 |
| `p_win` | 主周期净收益为正的概率 | 概率化解释和仓位折扣 |
| `downside_p10` | 悲观 10% 分位净收益 | 尾部风险约束 |
| `expected_drawdown` | 预测主周期最大不利波动 | 风险预算和止损压力评估 |
| `model_confidence` | 样本数、覆盖率、近期稳定性合成置信度 | 低置信候选降权或备选 |
| `rank_score` | 风险调整后的排序分 | 取代旧 `score` 的核心排序依据 |

适用边界:
- `tomorrow_picks`: 目标字段为 `next_close_return - execution_cost`。
- `swing_picks`: 目标字段为 `exit_return - execution_cost`。
- `short_term`: 继续保持观察池, 不在没有分钟级成交验证前输出可执行收益模型。

这个补充不是替代 2.1-2.3, 而是为它们统一优化方向: 所有因子、交互项、概率校准和 Meta-Labeling 最终都要服务于 `expected_return_net` 和风险调整后的 `rank_score`。

### 1.5 数据与执行层的隐藏问题（fresh review 新发现）

> 以下为第二轮源码 review 中发现的、**不属于模型层而属于数据/执行层**的结构性风险,
> 比模型权重调优更隐蔽, 会系统性高估策略表现或导致实盘与回测脱节。
> plan.md 此前版本未覆盖, 补充于此。

#### 1.5.1 【严重】幸存者偏差完全未处理

- **代码证据**: 全仓搜索 `survivorship` / `幸存者` / `delisted` / `退市.*样本` 返回 **0 结果**。
- **机制**: 验证库 (`strategy_validation.sqlite3`) 的信号来自推荐时的候选池; 但若某只股票此后停牌/退市/长期停牌, 其后续交易日收益**无法回填**到 `strategy_outcomes`, 导致该样本被自然剔除或收益缺失。
- **后果**: 验证指标 (胜率/平均收益) 系统性**偏高**, 因为亏损到退市的样本被悄悄移除了。这是量化中经典且最危险的偏差, 会让一个实际亏损的策略看起来盈利。
- **待办**: (1) 保留退市股的历史快照并按"最后一个交易日收盘价"回填; (2) 在 metrics 中区分"全样本(含退市)"与"存续样本"; (3) 增加退市率监控。

#### 1.5.2 【严重】尾盘入场执行价偏差未建模

- **代码证据**: `holding_discipline = "尾盘确认后入场"`, 主指标 `signal_next_close_return` 以**信号价**(尾盘价)作为入场价 (`backtest.py:61,156` 用当日收盘价作为 `entry_price`)。
- **现实**: 尾盘集合竞价 (14:57-15:00) 流动性有限, 实盘成交价会偏离信号价; 且**多只推荐股同时下单**会互相挤占流动性, 信号价越偏离实际成交价, 回测越乐观。
- **后果**: 回测净收益系统性高于实盘, 尤其是小盘股和尾盘集中下单场景。
- **待办**: 引入尾盘流动性惩罚 / VWAP 偏离滑点 (见 6.5.1), 并对成交额低于阈值的股票额外加收执行摩擦成本。

#### 1.5.3 【中】市场冲击未建模, 资金容量无上限

- **代码证据**: 交易成本模型 (`_execution_cost_pct`) 按成交额**静态分档**加滑点 (`VALIDATION_SLIPPAGE_*`), 但不考虑"自身下单对价格的冲击"。
- **现实**: 当资金量较大时, 买入小盘股会推高自身成本 (market impact), 经典近似为 `impact ∝ sqrt(资金量 / 日成交额)`。当前模型对此完全无感, 意味着策略**没有明确的资金容量上限**——资金量放大后回测仍显示盈利, 实盘却因冲击成本亏损。
- **待办**: 引入平方根市场冲击模型 (见 6.5.2), 并根据资金量动态调整最低成交额过滤阈值。

#### 1.5.4 【低】主题集中度未联动市场状态

- **代码证据**: `RECOMMENDATION_MAX_DISPLAY_PER_THEME = 3` 是固定常量, 不随 `market_regime` 变化。
- **现实**: risk_off 时板块轮动加剧、单一主题暴雷概率上升, 应比 risk_on 时更分散 (cap 调低); 但当前一律 3 只。
- **待办**: 按 regime 动态调整 theme_cap (见 6.5.3)。

### 1.6 统计与压力测试层的盲区（fresh review 第三轮发现）

> 以下问题不属于模型、数据或执行层, 而是**验证方法论**的盲区——会导致对策略有效性的误判。

#### 1.6.1 【中】缺乏黑天鹅/压力测试机制

- **代码证据**: 全仓搜索 `black_swan` / `stress_test` / `tail_event` / `黑天鹅` / `压力测试` / `极端事件` 返回 **0 结果**。
- **机制**: 当前风控是**反应式**的——回撤 > 8% 或胜率 < 48% 才退场。没有主动模拟极端情景（如 2015 年股灾、2024 年微盘崩盘、突发监管政策、美股隔夜暴跌传导）下策略会亏多少。
- **后果**: 策略可能在正常行情中表现尚可, 但在尾部事件中一次性回吐数年收益; 用户看到的"历史胜率"不包含这些低概率高冲击事件。
- **待办**: 建立情景压力测试框架 (见 6.6.1), 对历史最大回撤日、政策冲击日、流动性冻结日做回溯。

#### 1.6.2 【中】缺乏多重假设检验校正

- **代码证据**: `calibrate.py` 的坐标下降会对每个权重在 {0.7x, 1.0x, 1.3x} 三档扫描, 并组合多个目标函数项; 加上 walk-forward 的 4 折分割, 实际进行了大量隐式假设检验, 但代码中无 `multiple_testing` / `p_value` / `FDR` / `Bonferroni` 等处理。
- **机制**: 当测试大量参数组合时, 即使所有组合都是随机噪声, 也总会找到一些"表现显著"的组合。**不校正就会让过拟合结果看起来可信**。
- **后果**: walk-forward 中选出的"最优权重"可能只是数据挖掘的产物, 上线后快速失效。
- **待办**: 引入假发现率控制 (FDR) 或 Bonferroni 校正 (见 6.6.2), 尤其当扩展为 Ridge/交互项/Meta-Labeling 等更多超参数时。

### 1.7 收益服务审计: 每项改进必须能说明"为什么有利"

本计划中的机制不是都直接产生 alpha。为了避免把工程建设误写成收益来源, 每个机制必须至少服务以下五类目标之一:

1. **提升 alpha**: 更准确地区分未来净收益更高的候选。
2. **降低成本**: 减少滑点、市场冲击、不可成交和高换手损耗。
3. **降低风险**: 降低回撤、尾部损失、集中度和高波动暴露。
4. **提高验证真实性**: 避免幸存者偏差、未来函数、样本缺失和多重检验假发现。
5. **提升执行纪律**: 防止低置信模型、旧缓存或解释性字段绕过门控。

| 机制 | 收益服务类型 | 是否直接提升荐股/评分 | 处理原则 |
|------|--------------|----------------------|----------|
| 收益目标化排序 `rank_score` | 提升 alpha、降低风险 | 是, 但必须等样本和 OOS 通过 | `model_confidence=ready` 前只展示, 不接管排序 |
| 平滑风险扣分 | 降低风险、提升排序稳定性 | 是 | 默认开启, 防止边界样本分数跳变 |
| Sortino/下行分位目标 | 降低尾部风险 | 是 | 校准目标必须优先惩罚大亏结构 |
| 时间衰减 | 提升适应性 | 是 | 防止旧市场结构污染新权重 |
| 因子交互 | 潜在提升 alpha | 是, 但过拟合风险高 | 仅 shadow 评估, OOS/FDR 通过后灰度 |
| 概率校准 | 提升解释与仓位折扣 | 间接 | 可展示, 不能单独作为买入依据 |
| 分市场状态权重 | 提升适应性、降低风险 | 是, 但样本容易不足 | 用全局权重回退和状态内样本门槛保护 |
| Meta-Labeling | 降低错误交易 | 是, 但需要真实标签 | 默认 shadow, enforce 前必须通过 OOS/FDR |
| 多时间框架新因子 | 潜在提升 alpha | 待验证 | 先入特征库, 再看 RankIC/Top-K lift |
| 动态仓位缩放 | 降低风险 | 间接提升风险调整收益 | 可作为风控默认开启, 不改变股票排序 |
| 波动率目标化 | 降低回撤 | 间接提升风险调整收益 | 可默认开启, 防止高波动期满仓 |
| 组合相关性/主题 cap | 降低集中风险 | 间接提升风险调整收益 | 可默认开启, 但不使用低置信收益字段加仓 |
| 幸存者偏差修正 | 提高验证真实性 | 不直接提升收益 | 必须开启, 否则收益评估不可信 |
| 尾盘滑点/市场冲击 | 降低成本、提高验证真实性 | 间接 | 必须进入净收益标签和组合容量检查 |
| DeepSeek 事件/风险信号 | 潜在提升 alpha、降低错误交易 | 待验证 | 只能做结构化抽取、风险识别和解释; 未通过 OOS 前不得接管主排序 |
| 事件 alpha / Ensemble | 潜在提升 alpha | 待验证 | 单独归因, 未通过前不得接管主排序 |
| 压力测试/FDR | 提高验证真实性、降低过拟合 | 间接 | 作为生产灰度门槛, 不是收益来源 |
| 前端解释/看板 | 提升执行纪律 | 不直接 | 防止误把高分、概率、可执行动作混为一谈 |

结论: 文档中的大部分内容都能服务于收益, 但服务路径不同。**直接改变荐股排序或仓位的机制必须有真实前瞻证据; 间接机制可以先作为保守修正或解释层上线。**

---

## 二、改进方案

### 2.1 第一优先级: 低投入、高回报（预计 1 周）

#### 2.1.1 硬阈值规则平滑化

**现状** (`scoring.py:3878-3936`):

```python
if pct >= upper * 0.83:
    parts["intraday_chase"] = 12
elif pct >= upper * 0.72:
    parts["intraday_chase"] = 8

if amplitude >= 11:
    parts["amplitude"] = 10
elif amplitude >= 9.0:
    parts["amplitude"] = 4

if turnover_rate >= 18:
    parts["turnover_rate"] = 9
elif turnover_rate >= 14:
    parts["turnover_rate"] = 3

if volume_ratio >= 5:
    parts["volume_ratio"] = 10
elif volume_ratio >= 4:
    parts["volume_ratio"] = 5
```

**改进方案**: 用 **sigmoid/logistic 平滑函数**替代阶梯式硬编码。

核心思路：

```
continuous_penalty(x, threshold, max_penalty, steepness=2.0):
    return max_penalty / (1 + exp(-steepness * (x - threshold)))
```

具体映射:

| 惩罚项 | 原硬阈值 | 平滑阈值点 | 最大惩罚 | 陡峭度 |
|--------|---------|-----------|---------|--------|
| intraday_chase (追涨) | pct>=0.83*upper→12, pct>=0.72*upper→8 | upper*0.78 | 12 | 3.0 |
| amplitude (振幅) | >=11→10, >=9→4 | 10.0 | 10 | 1.5 |
| turnover_rate (换手) | >=18→9, >=14→3 | 16.0 | 9 | 1.0 |
| volume_ratio (量比) | >=5→10, >=4→5 | 4.5 | 10 | 2.0 |
| late_chase_speed (涨速) | >3.0→6, <-1.2→7 | ±2.1 | 7 | 2.0 |
| mid_gain_weak_close | 分段 | close_location<0.6 | 7 | 3.0 |
| weak_tail_close | <0.35→8, <0.45→10 | 0.40 | 10 | 4.0 |

实现位置: `_tomorrow_risk_penalty_parts()` 和 `_swing_risk_penalty_parts()`。

**实现辅助函数 (添加到 `scoring.py`)**:

```python
import math

def _smooth_penalty(value: float, threshold: float, max_penalty: float,
                    steepness: float = 2.0, direction: str = "above") -> float:
    """
    用 sigmoid 平滑替代硬阈值惩罚。

    Args:
        value: 当前指标值
        threshold: 阈值中点 (penalty = max_penalty/2 的点)
        max_penalty: 最大惩罚值
        steepness: 陡峭度 (越大越接近硬阈值)
        direction: "above" 表示 value 超过 threshold 时惩罚递增,
                   "below" 表示 value 低于 threshold 时惩罚递增

    Returns:
        [0, max_penalty] 之间的平滑惩罚值
    """
    if direction == "above":
        z = steepness * (value - threshold)
    else:
        z = steepness * (threshold - value)
    # 防止 exp 溢出
    z = max(-50.0, min(50.0, z))
    return max_penalty / (1.0 + math.exp(-z))
```

**向后兼容**: 新增 `config.USE_SMOOTH_PENALTY = True` 开关, 默认启用, 可通过环境变量关闭。

**预期效果**:
- 消除边界附近的"悬崖效应"
- 校准目标函数时梯度更平滑, 收敛更稳定
- 不影响极端情况 (远超阈值时惩罚趋近 `max_penalty`)

---

#### 2.1.2 目标函数引入下行风险惩罚

**现状** (`calibrate.py:86-122`):

```python
def _objective(metrics, strategy="", direction_focused=False):
    if strategy == "tomorrow_picks":
        win_rate = metrics["absolute_win_rate"]
        avg_return = metrics["absolute_avg_period_return"]
        median_return = metrics["absolute_median_period_return"]
        loss_quantile = metrics["absolute_loss_quantile_return"]
        avg_drawdown = metrics["absolute_avg_max_drawdown"]
        avg_open_gap = metrics["absolute_avg_next_open_return"]
        downside_penalty = min(0, loss_quantile) * 1.6 + min(0, avg_drawdown) * 0.25
        return (
            win_rate * 3.0 + avg_return * 0.6 + median_return * 0.2
            + avg_open_gap * 0.2 + downside_penalty
        )
    else:
        return win_rate + avg_return * 2.0
```

**局限**: `downside_penalty` 只用了亏损分位数和平均回撤, 未考虑:
- 亏损端的波动率 (大幅亏损的分散程度)
- 最大单笔亏损 (尾部极端风险)
- 正收益的稳定性

**改进方案**: 引入 **Sortino 比率** 和 **最大回撤惩罚**。

新增 `_validation_metrics` 需要暴露出每笔交易的 `return_series` 列表:

```python
def _objective(
    metrics: Dict[str, object],
    strategy: str = "",
    direction_focused: bool = False,
) -> float:
    if not metrics:
        return -1e9

    if strategy == "tomorrow_picks":
        win_rate = float(metrics.get("absolute_win_rate", 0.0) or 0.0)
        avg_return = float(metrics.get("absolute_avg_period_return", 0.0) or 0.0)
        median_return = float(metrics.get("absolute_median_period_return", 0.0) or 0.0)
        loss_quantile = float(metrics.get("absolute_loss_quantile_return", 0.0) or 0.0)
        avg_drawdown = float(metrics.get("absolute_avg_max_drawdown", 0.0) or 0.0)
        max_drawdown = float(metrics.get("absolute_max_drawdown", avg_drawdown) or 0.0)
        avg_open_gap = float(metrics.get("absolute_avg_next_open_return", 0.0) or 0.0)

        # --- 新增: 下行标准差 ---
        return_series = metrics.get("return_series") or []
        negative_returns = [r for r in return_series if r < 0]
        if len(negative_returns) >= 2:
            import statistics
            downside_std = statistics.stdev(negative_returns)
        else:
            downside_std = abs(avg_return) * 2.0 if avg_return < 0 else 2.0

        # Sortino 比率 (年化约 250 个交易日, 主周期约 1 天)
        sortino = avg_return / downside_std if downside_std > 1e-8 else 0.0

        # 尾部风险惩罚
        tail_risk = min(0.0, loss_quantile) * 1.6 + min(0.0, avg_drawdown) * 0.25 + min(0.0, max_drawdown) * 0.5

        # --- 新增: Sortino 项权重 ---
        sortino_weight = 0.4
        return (
            win_rate * 3.0
            + avg_return * 0.6
            + median_return * 0.2
            + avg_open_gap * 0.2
            + sortino * sortino_weight
            + tail_risk
        )
    else:
        win_rate = float(metrics.get("win_rate", 0.0) or 0.0)
        avg_return = float(metrics.get("avg_period_return", 0.0) or 0.0)
        # 2-5日也加入 Sortino
        return_series = metrics.get("return_series") or []
        negative_returns = [r for r in return_series if r < 0]
        downside_std = (
            __import__("statistics").stdev(negative_returns)
            if len(negative_returns) >= 2
            else (abs(avg_return) * 2.0 if avg_return < 0 else 2.0)
        )
        sortino = avg_return / downside_std if downside_std > 1e-8 else 0.0
        return win_rate * 1.5 + avg_return * 1.5 + sortino * 0.5
```

**配套变更**: `_evaluate_live_samples()` 和 `_metrics_from_ranked_groups()` 需在返回的 metrics 字典中加入 `"return_series"` 字段。

**预期效果**:
- 筛选出"收益分布偏正+亏损端紧凑"的参数组合
- 减少选择"偶尔大亏但小赚多次"的配置

---

#### 2.1.3 样本时间衰减加权

**现状**: walk-forward 折内所有训练样本权重相等。

**改进方案**: 指数衰减, 半衰期 60 个交易日。

在 `_fit_weights()` 的目标评估中嵌入时间衰减:

```python
import math
from datetime import datetime, timedelta

def _time_weighted_win_rate(returns_with_dates: list, half_life: int = 60) -> float:
    """按时间衰减计算加权胜率。"""
    if not returns_with_dates:
        return 0.0
    latest = max(item[1] for item in returns_with_dates)
    total_weight = 0.0
    weighted_positive = 0.0
    for ret, date_str in returns_with_dates:
        try:
            date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            date = latest
        days_ago = max(0, (latest - date).days)
        weight = 0.5 ** (days_ago / half_life)
        total_weight += weight
        if ret > 0:
            weighted_positive += weight
    return weighted_positive / total_weight if total_weight > 0 else 0.0


def _time_decay_objective(
    metrics: dict,
    return_series: list = None,
    signal_dates: list = None,
    half_life: int = 60,
) -> float:
    """在标准 objective 基础上乘以时间衰减加权因子。"""
    base = _objective(metrics)
    if not return_series or not signal_dates:
        return base
    # 近期表现因子: 1.0 表示近期=历史, >1 表示近期更好
    recent_half = half_life // 2  # 30 天
    paired = list(zip(return_series, signal_dates))
    overall_win = _time_weighted_win_rate(paired, half_life)
    recent_win = _time_weighted_win_rate(paired, recent_half)
    # 近期胜率比全局胜率高时加分
    recency_factor = 0.8 + 0.2 * (recent_win / max(0.01, overall_win))
    recency_factor = max(0.9, min(1.1, recency_factor))
    return base * recency_factor
```

**配置**: `CALIBRATE_TIME_DECAY_HALF_LIFE = 60` (可环境变量覆盖)

**预期效果**: 参数选择更倾向于近期表现稳定的配置, 对市场风格切换响应更快。

---

### 2.2 第二优先级: 中投入、中回报（预计 2-3 周）

#### 2.2.0 收益目标化排序层（本轮合并）

**核心思路**: 保留现有因子分、风险扣分和解释字段, 但最终排序不再直接使用旧 `score`, 而是使用由真实前瞻样本训练出来的风险调整收益分。

```text
expected_return_net = f(因子分, 风险分, 市场状态, 执行成本, 历史覆盖)
p_win               = P(primary_return_net > 0)
downside_p10        = Q10(primary_return_net)
rank_score          = 50
                      + expected_return_net * return_scale
                      + (p_win - 0.5) * probability_scale
                      + downside_p10 * downside_scale
                      - expected_drawdown * drawdown_scale
                      - uncertainty_penalty
```

**训练样本**:
- 来源: `StrategyValidationStore` 中已回填的真实前瞻样本。
- 特征: `raw_json` 里的 `liquidity_score`、`momentum_score`、`historical_edge_score`、`execution_score`、`tail_setup_score`、`risk_penalty`、`overheat_damp`、`regime_bonus`、`alphalite_coverage` 等。
- 标签:
  - `tomorrow_picks`: `next_close_return - _execution_cost_pct(row)`。
  - `swing_picks`: `exit_return - _execution_cost_pct(row)`。

**模型分层**:

| 样本条件 | 模型 | 原因 |
|----------|------|------|
| 真实成熟样本 < 60 个交易日 | 不启用收益模型, 沿用旧排序并输出 `model_confidence=low` | 样本不足时避免伪精确 |
| 60-120 个交易日 | Ridge / Huber 回归 + Isotonic 概率校准 | 稳健、可解释、低过拟合 |
| >120 个交易日且 OOS 通过 | Quantile 回归或 Meta-Labeling 辅助 | 增加下行分位与置信度建模 |

**最小可落地模块**:

```text
stock_analyzer/expected_return_model.py
  - build_training_samples(strategy)
  - train_expected_return_model(strategy, samples)
  - predict_expected_return(strategy, rows)
  - save/load model artifact

stock_analyzer/scoring.py
  - 保留旧 score/base_score 作为解释字段
  - 新增 rank_score/expected_return_net/p_win/downside_p10/model_confidence
  - 当前阶段只展示 shadow 字段, 线上排序仍使用旧 score
  - 只有 OOS/FDR 门控通过且显式开启 `ENABLE_EXPECTED_RETURN_RANKING` 后, 才允许 rank_score 替代旧排序
  - `model_confidence != ready` 时, 收益模型字段不得影响排序、组合权重或执行动作

stock_analyzer/calibrate.py
  - walk-forward 对比旧 score 与 rank_score 的 OOS 主周期净收益
  - 只有 OOS 改善和 FDR 门控通过才允许启用
```

**当前基础版实现**:
- `score_tomorrow_candidates()` / `score_swing_candidates()` 已支持可选 `expected_return_samples` 和 `use_expected_return_ranking`。
- 默认行为保持不变: `rank_score`、`expected_return_net`、`p_win`、`downside_p10` 继续作为 shadow/解释字段。
- `recommendation_runtime_support.expected_return_ranking_context()` 会在 `ENABLE_EXPECTED_RETURN_RANKING=1` 时读取验证样本, 检查真实交易日数、walk-forward OOS、FDR/sign-test guard。
- 只有门控通过时, 运行层才向策略函数传入 `use_expected_return_ranking=True`, 让 `rank_score` 替代旧 `score` 的排序顺序；未通过时只传入样本用于 shadow 估计。
- 当前基础版尚未落地持久化模型 artifact, 仍是运行时基于验证样本的轻量估计；后续再升级为 Ridge/Huber/Quantile artifact。

**上线门槛**:
- `rank_score` Top-K 的 OOS 平均净收益必须高于旧 `score` Top-K。
- `rank_score` Top-K 的 95% 置信下界不能低于旧模型。
- 执行跳过率、最大回撤、主题集中度不得恶化。
- 未通过时只展示预测字段, 不用于排序和执行。

#### 2.2.1 因子交互项 (二阶多项式)

**核心思路**: 当前 `score = Σ(w_i × f_i)`, 扩展为 `score = Σ(w_i × f_i) + Σ(w_ij × f_i × f_j)`。

**关键交互对**:

| 交互项 | 含义 | 预期效应 |
|--------|------|---------|
| `momentum × liquidity` | 动量需成交验证 | 高动量低成交 → 降权 |
| `execution × tail_setup` | 买入安全需尾盘确认 | 双重安全 → 加权 |
| `historical_edge × momentum` | 趋势延续需短期动能 | 共振信号 → 加权 |
| `liquidity × tail_setup` | 尾盘结构需流动性支撑 | 缺流动性 → 降权 |
| `momentum × risk_penalty` | 动量高但风险也高 | 对冲关系 |

**当前基础版实现**: 先落地无新增依赖的 shadow evaluator, 不改变线上排序。

```python
def evaluate_interaction_ranker(strategy, samples, top_k=10):
    """
    逐折训练二阶交互项, 在测试折重排 Top-K, 输出 shadow OOS 对比。

    当前实现不依赖 sklearn:
    1. 从策略 combiner 中抽取组件因子, 额外加入 risk_penalty。
    2. 因子值归一化到 [-1, 1], 构造少量二阶 pair。
    3. 在训练折里用日内中位收益去市场日效应, 估计 pair 与净收益的方向/强度。
    4. 在测试折上只给旧 score 加有限幅度的 interaction delta。
    5. 输出 baseline_oos_objective、interaction_oos_objective、positive_folds、FDR 状态。
    """
```

**后续增强方案**: 样本量足够且依赖允许后, 再升级为 Ridge/Huber + PolynomialFeatures, 并对 `alpha=[0.1, 0.5, 1.0, 2.0, 5.0]` 做 walk-forward 选择。

**通过 walk-forward 验证**: 每折只用训练集拟合交互项, 在测试集上与 baseline 线性模型对比 OOS 改善。

**启用门槛**:
- `interaction_oos_objective > baseline_oos_objective + CALIBRATE_IMPROVE_MARGIN`
- `positive_folds > fold_count / 2`
- 若开启 `ENABLE_CALIBRATE_FDR`, FDR/sign-test guard 必须通过
- 未通过时只输出 shadow 评估, 不写生产权重, 不替代旧排序

**预期效果**:
- 捕捉因子间的非线性关系
- 基础版通过 pair 数量、分数增量 cap 和 OOS 门控减少过拟合
- 后续 Ridge 正则化进一步压低高维交互过拟合
- 只在 OOS 显著改善时启用

---

#### 2.2.2 分数→概率校准

**核心思路**: 将排序分转化为"历史上同类分数的股票次日正收益比例"。

**实现方案**: 先采用无新增依赖的"分桶胜率 + 单调修正"校准器, 后续样本充足且引入 sklearn 依赖后再升级为 Isotonic Regression。

```python
class ScoreCalibrator:
    """分数-概率校准器, 用历史真实结果训练。"""

    def fit(self, samples):
        # 输入: stored_score/decision_score + primary_return_net
        # 1) 按 score 排序分桶
        # 2) 每桶统计主周期净收益为正的比例
        # 3) 用 PAVA 思路做单调修正, 保证高分桶概率不低于低分桶

    def predict(self, score):
        return {
            "calibrated_probability": 0.61,   # 0-1
            "probability_label": "中等置信",
            "probability_sample_count": 36,
            "probability_bucket": "65-75",
            "probability_avg_return": 0.42,
        }
```

**集成方式**:

```python
# 在 app_support.attach_score_calibration() 中
from .probability_calibration import apply_score_calibration, load_calibrator, train_score_calibrator

calibrator = load_calibrator(strategy_name)
if not calibrator.is_fitted:
    calibrator = train_score_calibrator(strategy_name, live_weight_samples)
apply_score_calibration(result_rows, calibrator)
```

**训练触发**: 每次 `calibrate_live_weights()` 成功写入后, 用验证库样本重新训练校准器并保存到 `.runtime/score_calibrator_{strategy}.json`; 页面运行时若未找到持久化文件, 会用最近验证样本临时训练并附加解释字段。

**前端展示**: `score_note` 从 `"综合分是量价/趋势/风险排序分，不等于上涨概率"` 升级为:
```
score_note: "综合分 72.0，历史同类信号主周期正收益概率约 61.0%（N样本）"
```

---

#### 2.2.3 分市场状态独立模型

**核心思路**: 不同盘面环境用不同权重/模型。

**三种状态定义** (沿用现有 `build_market_regime()`):

| 状态 | regime_score | 特征 | 策略倾向 |
|------|-------------|------|---------|
| risk_on | ≥ 68 | 领涨股多、市场宽度好、强势股比例高 | 动量/突破主导 |
| balanced | 42-67 | 中性市场 | 均衡权重 |
| risk_off | ≤ 41 | 领涨股稀缺、下跌比例高 | 防御主导 (流动性/质量) |

**实现**:

```python
# config.py 新增
REGIME_WEIGHT_SETS = {
    "tomorrow_picks": {
        "risk_on": {
            "liquidity": 0.25,
            "momentum": 0.25,     # ↑ 动量放大
            "historical_edge": 0.22,  # ↑ 趋势放大
            "execution": 0.13,
            "tail_setup": 0.15,
        },
        "balanced": {
            "liquidity": 0.30,
            "momentum": 0.20,
            "historical_edge": 0.20,
            "execution": 0.15,
            "tail_setup": 0.15,
        },
        "risk_off": {
            "liquidity": 0.38,     # ↑↑ 流动性最重
            "momentum": 0.14,      # ↓ 动量压缩
            "historical_edge": 0.15,
            "execution": 0.20,     # ↑ 买入安全放重
            "tail_setup": 0.13,
        },
    },
    # swing_picks 同理...
}
```

**当前基础版实现**: 先落地 `evaluate_regime_specific_weights()` shadow evaluator。
- 从验证样本 `raw.regime_level`、`raw.market_regime.level` 或 `regime_score` 识别 `risk_on/balanced/risk_off`。
- walk-forward 每折只用训练集按状态拟合权重, 测试集按样本状态选择权重重排 Top-K。
- 某个状态训练样本数低于 `REGIME_SPECIFIC_MIN_TRAIN_SAMPLES` 时, 该状态回退全局权重。
- 只输出 `weights_by_regime`、`regime_oos_objective`、`positive_folds`、`fdr` 和 `can_promote`; 不直接写生产权重。

**启用门槛**:
- `regime_oos_objective > baseline_oos_objective + CALIBRATE_IMPROVE_MARGIN`
- `positive_folds > fold_count / 2`
- 若开启 `ENABLE_CALIBRATE_FDR`, FDR/sign-test guard 必须通过
- 所有关键状态样本充足, 否则不足状态继续回退全局权重

**配置开关**: `ENABLE_REGIME_SPECIFIC_WEIGHTS = True` (默认关闭, 需足够样本后才开启), `REGIME_SPECIFIC_MIN_TRAIN_SAMPLES = 20`

---

### 2.3 第三优先级: 高投入、高回报（预计 3-4 周）

#### 2.3.1 Meta-Labeling (元标签)

**核心思路**: 二层模型。

```
第一层 (主模型): 选股 → "这只股票的综合打分是 72 分"
第二层 (元模型): 预测主模型本次判断是否正确 → "置信度 78%"
最终决策: confidence < 60% → 降低仓位或跳过
```

**特征工程** (元模型输入):

| 类别 | 特征 | 说明 |
|------|------|------|
| **主模型分解** | liquidity_score, momentum_score, historical_edge_score, execution_score, tail_setup_score | 各维度原始分 |
| **主模型交叉** | score × momentum, risk_penalty / score | 交互特征 |
| **主模型历史准确率** | 最近 N 次该分数段的正确率 | 模型近期表现 |
| **市场环境** | regime_score, regime_level, breadth_pct | 盘面氛围 |
| **个股个性** | volatility_20d, sixty_day_pct, turnover_rate | 与风险的交互 |
| **时间特征** | weekday(周几), month(月份), 距财报季天数 | 日历效应 |
| **板块特征** | sector_relative_strength, sector_heat | 板块轮动 |

**标签定义**:
```
y = 1 if primary_return_net > 0  else 0
```
(主模型判断正确 = 推荐后次日正收益)

**当前基础版实现**: 先落地无新增依赖的可解释 meta confidence, 默认只输出 shadow 字段。

```python
stock_analyzer/meta_labeling.py
  - train_meta_label_model(strategy, samples)
  - predict_meta_confidence(row, model)
  - apply_meta_labeling(rows, model, enforce=False)

stock_analyzer/calibrate.py
  - evaluate_meta_labeling_gate(strategy, samples)
  - walk-forward 对比旧 score Top-K 与 meta confidence gate 后 Top-K
  - 只有 OOS/FDR 通过才允许 `META_LABELING_ENFORCE_ACTION=1`
```

当前基础版使用:
- `score` 分桶历史胜率
- `risk_penalty` 分桶历史胜率
- 全样本正收益率
- 行上已有 `calibrated_probability` / `p_win` 作为辅助校准

输出字段:
- `meta_labeling.confidence`
- `meta_labeling.action`: `full` / `reduced` / `skip`
- `meta_labeling.position_scale`
- `meta_confidence`

**后续增强方案**: 样本和依赖成熟后, 再升级 LightGBM/树模型二分类器, 处理更多非线性特征和缺失值。

**集成到现有流程**:
- 当前接入 `app_support.attach_meta_labeling()`, 在 `attach_validation_summary()` 后附加 shadow 字段。
- 默认不改 `trade_action`, 不跳过、不降仓。
- 只有同时开启 `ENABLE_META_LABELING=1` 和 `META_LABELING_ENFORCE_ACTION=1` 时, 才允许按 action 调整仓位或降为观察。

**训练与部署**:
- 当前运行时使用最近验证样本即时训练轻量模型。
- 后续若升级树模型, 再持久化到 `.runtime/meta_model_{strategy}.pkl/json`。
- OOS 验证: 对比是否使用元模型门控的 walk-forward metrics, 通过后才允许 enforce。

**启用门槛**:
- `meta_oos_objective > baseline_oos_objective + CALIBRATE_IMPROVE_MARGIN`
- `positive_folds > fold_count / 2`
- 若开启 `ENABLE_CALIBRATE_FDR`, FDR/sign-test guard 必须通过
- 未通过时只展示 `meta_labeling` / `meta_confidence`, 不改变执行动作

---

#### 2.3.2 多时间框架特征工程

**新增因子** (在 `factors.py` 中扩展):

```python
# === 日频增强因子 ===

def compute_intraday_factors(history: pd.DataFrame) -> dict:
    """
    基于日线 OHLCV 计算日内结构因子。可用于无 tick 数据的场景。
    """
    if history is None or history.empty or len(history) < 2:
        return {}

    close = history["close"]
    high = history["high"]
    low = history["low"]
    open_price = history["open"]

    latest = close.iloc[-1]

    # 1. 日内均价偏离 (收盘 vs 均价)
    typical_price = (high.iloc[-1] + low.iloc[-1] + close.iloc[-1]) / 3.0
    if typical_price > 0:
        close_vs_vwap = (close.iloc[-1] / typical_price - 1.0) * 100
    else:
        close_vs_vwap = 0.0

    # 2. 上影线/下影线比例
    if high.iloc[-1] > low.iloc[-1]:
        upper_wick = (high.iloc[-1] - max(open_price.iloc[-1], close.iloc[-1]))
        lower_wick = (min(open_price.iloc[-1], close.iloc[-1]) - low.iloc[-1])
        body = abs(close.iloc[-1] - open_price.iloc[-1])
        range_val = high.iloc[-1] - low.iloc[-1]
        upper_wick_ratio = upper_wick / range_val if range_val > 0 else 0
        lower_wick_ratio = lower_wick / range_val if range_val > 0 else 0
    else:
        upper_wick_ratio = lower_wick_ratio = 0.0

    # 3. 近期 N 日价格位置
    n = 20
    if len(close) >= n:
        high_n = high.tail(n).max()
        low_n = low.tail(n).min()
        price_position_20d = (
            (latest - low_n) / (high_n - low_n) * 100
            if high_n > low_n else 50.0
        )
    else:
        price_position_20d = 50.0

    # 4. 连涨/连跌天数
    consecutive_days = _consecutive_direction(close)

    # 5. 近期 N 日振幅均值
    amplitude_5d = _avg_amplitude(high.tail(5), low.tail(5), close.tail(5))

    # 6. 真实波幅 (ATR)
    atr_14 = _compute_atr(high, low, close, period=14)

    return {
        "close_vs_vwap": round(close_vs_vwap, 4),
        "upper_wick_ratio": round(upper_wick_ratio, 4),
        "lower_wick_ratio": round(lower_wick_ratio, 4),
        "price_position_20d": round(price_position_20d, 2),
        "consecutive_up_days": consecutive_days.get("up", 0),
        "consecutive_down_days": consecutive_days.get("down", 0),
        "amplitude_5d_mean": round(amplitude_5d, 4),
        "atr_14_pct": round(atr_14 / latest * 100, 4) if latest > 0 else 0,
    }
```

**板块相对强度** (新增独立模块 `sector_relative.py`):

```python
def sector_relative_strength(code: str, daily_return: float,
                              sector_returns: dict) -> dict:
    """
    计算个股相对板块的超额收益。
    Args:
        sector_returns: {"板块名": 板块内所有股票的中位数日收益}
    """
    sector = _classify_sector(code)  # 通过代码前缀/名称分类
    sector_median = sector_returns.get(sector, 0.0)
    relative_strength = daily_return - sector_median
    sector_rank = _sector_percentile(code, sector)
    return {
        "sector": sector,
        "sector_relative_strength": round(relative_strength, 4),
        "sector_rank_pct": round(sector_rank, 2),
    }
```

**因子纳入策略**:

| 新因子 | 适用策略 | 维度归属 |
|--------|---------|---------|
| close_vs_vwap | 明日优先 | tail_setup (尾盘结构) |
| upper_wick_ratio↓ | 明日优先 | execution (卖出压力) |
| lower_wick_ratio↑ | 2-5日 | trend (支撑确认) |
| price_position_20d | 2-5日 | trend/momentum |
| consecutive_up_days | 全部 | risk_guard (过度延伸) |
| amplitude_5d_mean | 全部 | risk_guard |
| sector_relative_strength | 全部 | industry/cross_section |

**当前落地**:
- `factors.py` 已扩展 `ALPHALITE_COLUMNS`, 输出 `close_vs_vwap`、`upper_wick_ratio`、`lower_wick_ratio`、`price_position_20d`、`consecutive_up_days`、`consecutive_down_days`、`amplitude_5d_mean`。
- `close_vs_vwap` 在无分钟 VWAP 数据时用日线 typical price `(high + low + close) / 3` 近似, 仅作为结构因子。
- 默认 `ENABLE_ENHANCED_FACTORS=0` 时这些列保持 0, 不改变现有评分。
- 开启后只进入因子表和验证样本, 是否纳入 `tail_setup`/`execution`/`risk_guard` 仍需 OOS 验证。

**配置开关**: `ENABLE_ENHANCED_FACTORS = True`, 通过环境变量控制, 默认先关闭, OOS 验证通过后开启。

---

#### 2.3.3 动态仓位缩放

**现状**: 策略退场是二元的 (胜率<48% → 全部禁止)。

**改进**: 渐变降权, 连续调整仓位比例。

**当前落地**:
- `strategy_validation_gate_decision()` 在未阻断时返回 `position_scale` 与 `position_scale_reason`。
- `apply_strategy_validation_gate()` 对通过门控的行调用 `apply_position_scale()`, 将 `trade_action.position_size` 乘以连续缩放因子。
- `retired`、样本不足、真实收益/胜率/回撤/CI 不达标仍然硬阻断并降为备选观察, 不做半仓放行。

```python
def dynamic_position_scaling(
    strategy_metrics: dict,
    base_position: float = 1.0,
) -> dict:
    """
    根据策略近期的多个指标, 连续调整建议仓位比例。

    Returns:
        {
            "scale": 0.0-1.0 仓位缩放因子,
            "components": {各分项贡献},
            "reason": 调整原因,
        }
    """
    win_rate = coerce_number(strategy_metrics.get("real_win_rate_primary_net"), 50.0)
    avg_return = coerce_number(strategy_metrics.get("real_avg_primary_return_net"), 0.0)
    drawdown = coerce_number(strategy_metrics.get("real_avg_max_drawdown_primary"), -5.0)
    sample_count = int(strategy_metrics.get("real_sample_count") or 0)

    # 组件 1: 胜率缩放 (40%→0, 60%→1, 线性)
    win_scale = max(0.0, min(1.0, (win_rate - 40.0) / 20.0))

    # 组件 2: 收益缩放 (-1%→0, +1%→1, 线性)
    return_scale = max(0.0, min(1.0, (avg_return + 1.0) / 2.0))

    # 组件 3: 回撤缩放 (-10%→0, -3%→1)
    dd_scale = max(0.0, min(1.0, (drawdown + 10.0) / 7.0))

    # 组件 4: 样本充分度 (20→0.5, 60→1.0)
    sample_scale = max(0.5, min(1.0, sample_count / 60.0))

    # 综合 (各组件等权)
    scale = (win_scale * 0.35 + return_scale * 0.30 + dd_scale * 0.25 + sample_scale * 0.10)

    return {
        "scale": round(scale, 4),
        "components": {
            "win_rate": round(win_scale, 4),
            "avg_return": round(return_scale, 4),
            "drawdown": round(dd_scale, 4),
            "sample_sufficiency": round(sample_scale, 4),
        },
        "reason": _scale_reason(scale),
        "base_position": base_position,
        "adjusted_position": round(base_position * scale, 4),
    }


def _scale_reason(scale: float) -> str:
    if scale >= 0.8:
        return "策略表现稳健, 标准仓位"
    elif scale >= 0.5:
        return "策略表现一般, 适度降仓"
    elif scale >= 0.25:
        return "策略近期走弱, 大幅降仓"
    else:
        return "策略持续不佳, 建议暂停"
```

**与现有退场机制的配合**:
- 保留 hard floor: 当 `scale < 0.15` 时, 行为等同于当前 `retired` 状态 (禁止执行)
- 中间状态 (0.15-1.0): 连续调整, 替代当前的二元开关

---

## 三、启用与实施路线图

本轮先只修改本文档, 不改代码。后续真正改代码时, 按"保守修正先开、预测模型后验"的顺序推进。

### 3.1 可用能力启用分层

| 分层 | 能力 | 目标默认状态 | 理由 | 约束 |
|------|------|--------------|------|------|
| 第一批: 验证口径修正 | 幸存者偏差修正、尾盘滑点、市场冲击成本、主题集中度联动 | 开启 | 这些能力主要修正过度乐观的验证结果, 会让收益口径更保守 | 开启后必须重新生成 baseline, 不能和旧指标直接比较 |
| 第一批: 保守风控 | 平滑风险扣分、动态仓位缩放、波动率目标化、组合相关性 cap | 开启 | 这些能力降低悬崖式跳变、过度集中和高波动暴露 | 不允许使用低置信收益模型字段做加仓依据 |
| 第一批: 统计保护 | Sortino/下行目标、时间衰减、FDR/sign-test guard、压力测试输出 | 开启或随校准启用 | 降低过拟合和尾部风险误判 | FDR 无显著结果时必须回退默认权重 |
| 第二批: 解释型输出 | 概率校准、`expected_return_net/p_win/downside_p10/rank_score` shadow 字段 | 开启展示 | 改善用户理解和后续验证数据采集 | `model_confidence != ready` 时不得影响排序、仓位或执行 |
| 第三批: 生产排序/门控 | 收益目标化排序、因子交互、分状态权重、Meta-Labeling enforce | 默认关闭 | 这些能力会改变候选排序或执行动作 | 满足真实样本数、OOS、FDR、CI 下界后才灰度 |
| 第四批: 新 alpha 源 | 事件 alpha、Ensemble、多时间框架因子进入主模型 | 默认关闭或 shadow | 新信号源容易引入主观性和数据漂移 | 必须单独归因、单独 OOS、单独回退 |

### 3.2 文档计划后的代码实施顺序

```
第 A 步 ── 打开已实现的保守修正
    ├─ 6.5.4 幸存者偏差修正
    ├─ 6.5.1 尾盘执行价滑点
    ├─ 6.5.2 市场冲击模型
    ├─ 6.5.3 主题集中度联动
    ├─ 2.1.1 平滑风险扣分
    ├─ 2.1.2/2.1.3 Sortino + 时间衰减
    └─ 2.3.3/6.2.1/6.2.2 动态仓位 + 波动率目标化 + 相关性 cap
            ↓ 重新基线化所有策略真实 metrics

第 B 步 ── 开启解释型 shadow 输出
    ├─ 2.2.2 分数→概率校准
    ├─ 2.2.0 expected_return_net/p_win/downside_p10/rank_score shadow
    └─ 前端明确展示"影子/低置信/就绪"状态
            ↓ 不改变线上排序、不改变组合权重、不改变执行动作

第 C 步 ── OOS/FDR 后灰度生产排序
    ├─ 2.2.0 收益目标化排序
    ├─ 2.2.1 因子交互
    ├─ 2.2.3 分市场状态权重
    └─ 2.3.1 Meta-Labeling enforce
            ↓ 仅在真实样本数、OOS、FDR、CI 下界全部通过后启用

第 D 步 ── 新 alpha 源单独归因
    ├─ 6.2.3 事件 alpha
    ├─ 6.2.4 Ensemble
    └─ 2.3.2 多时间框架因子入主模型
            ↓ 单独归因通过前不得接管主排序
```

每个阶段完成后:
1. 运行 `calibrate_live_weights()` 或对应 shadow evaluator 对比改进前后的 OOS metrics。
2. 仅在 OOS 客观指标显著改善、positive_folds 过半且 FDR/sign-test guard 通过时, 才允许进入生产排序或执行门控。
3. 更新 `strategy_and_prediction.md` 中的策略描述。
4. **第 A 步完成后必须重新基线化**: 记录修正后的真实胜率/收益, 作为后续所有改进的对比基准。
5. 任一开关导致真实前瞻指标、执行跳过率、回撤或主题集中度恶化, 立即回退对应开关。

---

## 四、验证与回退机制

### 4.1 验证标准

每个改进必须通过以下检验:

| 检验项 | 阈值 | 说明 |
|--------|------|------|
| OOS objective 改善 | > 0.05 | 样本外目标函数提升 |
| positive_folds | > fold_count / 2 | 多数折正面 |
| FDR/sign-test guard | 通过 | 参数扫描或模型灰度前必须降低假发现率 |
| 95% CI 下界 | 不低于 baseline | 期望收益模型不能只看均值改善 |
| real_win_rate 不降 | ≥ baseline | 真实胜率至少不降 |
| real_avg_return 不降 | ≥ baseline | 真实平均收益至少不降 |
| 模型复杂度增量 | 可接受 | 不因增加参数导致严重过拟合 |

### 4.2 回退开关

所有新机制均通过 `config.py` 环境变量控制。目标默认状态按 3.1 分层执行: 保守修正与统计保护优先开启, 改变排序/执行的预测模型保持关闭。

```python
# 2.1.1
USE_SMOOTH_PENALTY = os.getenv("USE_SMOOTH_PENALTY", "1") == "1"

# 2.1.2
CALIBRATE_USE_SORTINO = os.getenv("CALIBRATE_USE_SORTINO", "1") == "1"

# 2.1.3
CALIBRATE_USE_TIME_DECAY = os.getenv("CALIBRATE_USE_TIME_DECAY", "1") == "1"
CALIBRATE_TIME_DECAY_HALF_LIFE = int(os.getenv("CALIBRATE_TIME_DECAY_HALF_LIFE", "60"))

# 2.2.0
# 继续默认关闭: 只允许 shadow 字段展示, 不替代旧 score 排序
ENABLE_EXPECTED_RETURN_RANKING = os.getenv("ENABLE_EXPECTED_RETURN_RANKING", "0") == "1"
EXPECTED_RETURN_MIN_REAL_DAYS = int(os.getenv("EXPECTED_RETURN_MIN_REAL_DAYS", "60"))
EXPECTED_RETURN_MIN_OOS_DELTA = float(os.getenv("EXPECTED_RETURN_MIN_OOS_DELTA", "0.05"))

# 2.2.1
ENABLE_INTERACTION_TERMS = os.getenv("ENABLE_INTERACTION_TERMS", "0") == "1"
INTERACTION_TERM_MAX_PAIRS = int(os.getenv("INTERACTION_TERM_MAX_PAIRS", "8"))
INTERACTION_MIN_TRAIN_SAMPLES = int(os.getenv("INTERACTION_MIN_TRAIN_SAMPLES", "20"))
INTERACTION_SCORE_SCALE = float(os.getenv("INTERACTION_SCORE_SCALE", "6.0"))
INTERACTION_SCORE_DELTA_CAP = float(os.getenv("INTERACTION_SCORE_DELTA_CAP", "12.0"))
INTERACTION_MIN_ABS_CORR = float(os.getenv("INTERACTION_MIN_ABS_CORR", "0.02"))

# 2.2.2
SCORE_CALIBRATOR_DIR = os.getenv("SCORE_CALIBRATOR_DIR", ".runtime")
SCORE_CALIBRATION_MIN_SAMPLES = int(os.getenv("SCORE_CALIBRATION_MIN_SAMPLES", "20"))
SCORE_CALIBRATION_BUCKETS = int(os.getenv("SCORE_CALIBRATION_BUCKETS", "5"))

# 2.2.3
ENABLE_REGIME_SPECIFIC_WEIGHTS = os.getenv("ENABLE_REGIME_SPECIFIC_WEIGHTS", "0") == "1"
REGIME_SPECIFIC_MIN_TRAIN_SAMPLES = int(os.getenv("REGIME_SPECIFIC_MIN_TRAIN_SAMPLES", "20"))

# 2.3.1
ENABLE_META_LABELING = os.getenv("ENABLE_META_LABELING", "0") == "1"
META_LABELING_MIN_SAMPLES = int(os.getenv("META_LABELING_MIN_SAMPLES", "50"))
META_LABELING_FULL_THRESHOLD = float(os.getenv("META_LABELING_FULL_THRESHOLD", "0.65"))
META_LABELING_REDUCED_THRESHOLD = float(os.getenv("META_LABELING_REDUCED_THRESHOLD", "0.50"))
META_LABELING_ENFORCE_ACTION = os.getenv("META_LABELING_ENFORCE_ACTION", "0") == "1"

# 2.3.2
ENABLE_ENHANCED_FACTORS = os.getenv("ENABLE_ENHANCED_FACTORS", "0") == "1"

# 2.3.3
ENABLE_DYNAMIC_POSITION_SCALING = os.getenv("ENABLE_DYNAMIC_POSITION_SCALING", "1") == "1"
STRATEGY_POSITION_SCALE_MIN = float(os.getenv("STRATEGY_POSITION_SCALE_MIN", "0.35"))
STRATEGY_POSITION_SCALE_PROBATION = float(os.getenv("STRATEGY_POSITION_SCALE_PROBATION", "0.6"))

# 6.2.1 / P2
ENABLE_VOLATILITY_TARGETING = os.getenv("ENABLE_VOLATILITY_TARGETING", "1") == "1"
PORTFOLIO_TARGET_VOLATILITY_PCT = float(os.getenv("PORTFOLIO_TARGET_VOLATILITY_PCT", "5.0"))
PORTFOLIO_VOL_SCALE_MIN = float(os.getenv("PORTFOLIO_VOL_SCALE_MIN", "0.35"))
PORTFOLIO_VOL_SCALE_MAX = float(os.getenv("PORTFOLIO_VOL_SCALE_MAX", "1.15"))

# 6.2.2
ENABLE_PORTFOLIO_OPTIMIZATION = os.getenv("ENABLE_PORTFOLIO_OPTIMIZATION", "1") == "1"
PORTFOLIO_CORRELATION_GROUP_CAP = float(os.getenv("PORTFOLIO_CORRELATION_GROUP_CAP", "0.45"))

# 6.2.3 / 6.2.4
ENABLE_EVENT_ALPHA = os.getenv("ENABLE_EVENT_ALPHA", "0") == "1"
EVENT_ALPHA_MIN_SCORE = float(os.getenv("EVENT_ALPHA_MIN_SCORE", "60"))
ENABLE_ENSEMBLE = os.getenv("ENABLE_ENSEMBLE", "0") == "1"
ENSEMBLE_MODEL_WEIGHTS = os.getenv("ENSEMBLE_MODEL_WEIGHTS", "")

# 6.5 数据与执行层修正
ENABLE_SURVIVORSHIP_CORRECTION = os.getenv("ENABLE_SURVIVORSHIP_CORRECTION", "1") == "1"
ENABLE_TAIL_AUCTION_SLIPPAGE = os.getenv("ENABLE_TAIL_AUCTION_SLIPPAGE", "1") == "1"
ENABLE_MARKET_IMPACT = os.getenv("ENABLE_MARKET_IMPACT", "1") == "1"
ENABLE_REGIME_THEME_CAP = os.getenv("ENABLE_REGIME_THEME_CAP", "1") == "1"

# 6.6 统计与压力测试修正
ENABLE_STRESS_TEST = os.getenv("ENABLE_STRESS_TEST", "1") == "1"
STRESS_TEST_SCENARIOS_PATH = os.getenv("STRESS_TEST_SCENARIOS_PATH", ".runtime/stress_scenarios.json")
ENABLE_CALIBRATE_FDR = os.getenv("ENABLE_CALIBRATE_FDR", "1") == "1"
CALIBRATE_FDR_Q = float(os.getenv("CALIBRATE_FDR_Q", "0.1"))
```

回退规则:
- 第一批默认开启项可单独设为 `"0"` 回退, 但回退后生成的验证指标不得和修正后 baseline 混用。
- `ENABLE_PORTFOLIO_OPTIMIZATION=1` 只表示启用相关性/集中度约束; 在收益模型 `model_confidence != ready` 时, 不得使用 `expected_return_net` 或 `p_win` 做加仓倾斜。
- `ENABLE_EXPECTED_RETURN_RANKING`、`ENABLE_INTERACTION_TERMS`、`ENABLE_REGIME_SPECIFIC_WEIGHTS`、`META_LABELING_ENFORCE_ACTION`、`ENABLE_EVENT_ALPHA`、`ENABLE_ENSEMBLE` 保持默认关闭, 只有 OOS/FDR/CI 门控通过后才允许灰度。

---

## 五、风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|---------|
| 期望收益模型伪精确 | 高 | 未满 60 个真实交易日不启用排序; OOS/FDR 不通过时只展示预测字段 |
| 交互项/Ridge 过拟合 | 中 | L2 正则化 + walk-forward OOS 验证 |
| 概率校准过拟合 | 低 | 分桶胜率 + 单调保序修正 + 最少 20 样本才拟合 |
| 分状态模型样本不足 | 中 | risk_off/risk_on 样本 < 30 时回退到 balanced 权重 |
| Meta-Labeling 过拟合 | 高 | 严格控制树深度(4)+叶子数(15), walk-forward 验证 |
| 新因子数据覆盖率不足 | 中 | 因子不可用时回退为中性值 50.0 |
| 复杂度增加导致维护困难 | 低 | 全部新功能有独立开关, 可逐模块回退 |
| **幸存者偏差修正后真实指标下降** | 中 | 这是预期内的"挤水分"; 只有修正后仍达标的策略才上线 |
| **尾盘滑点修正导致部分小盘股被剔除** | 低 | 可接受, 本就应剔除流动性不足的标的 |
| **市场冲击模型系数难标定** | 中 | 先用保守系数 0.1, 用实盘成交数据回溯校准 |
| **组合优化在小样本下协方差矩阵不稳定** | 中 | 用 Ledoit-Wolf 收缩估计或对角占优回退 |
| **压力测试场景选择偏误** | 中 | 覆盖多类尾部事件(流动性/政策/外部冲击), 定期更新场景库 |
| **FDR 校正导致无配置可选** | 低 | 无显著配置时回退默认权重, 不强制使用 Ridge/交互项 |
| **多重比较下假发现激增** | 高 | 2.2.0/2.2.1/2.3.1 启用时必须同步开启 `ENABLE_CALIBRATE_FDR` |

---

## 六、超越现有框架的更优荐股逻辑（新 alpha 源）

> 第二章的改进仍在"优化现有量价因子加权"的范式内, 属于**地基改进**。
> 本章提出**改变 alpha 来源**的更根本升级——因为纯量价因子已被市场高度套利,
> 真正的收益天花板突破需要引入不相关的新 alpha 源与组合层优化。
> 注意: "更优"= 提升**风险调整后收益 (Sharpe/Sortino)** 与**稳健性**, 不是"保证收益"。

### 6.1 新逻辑总览与优先级

| # | 新逻辑 | 核心思想 | 为何可能更优 | 难度 | 优先级 |
|---|--------|---------|-------------|------|--------|
| A | **波动率目标化仓位** | 高波动期降仓, 低波动期加仓 | A股波动聚类显著; 控回撤=变相提收益 | 低 | 🥇 最高 |
| B | **组合层 Max-Sharpe 优化** | 选"一篮子"而非逐只排序, 加相关性约束 | 当前忽略个股相关性, 组合层可分散风险 | 中 | 🥇 最高 |
| C | **催化剂/事件 alpha 独立化** | 事件驱动信号独立成源, 与量价并行 | 事件因果链比量价更清晰、被套利更少 | 中 | 🥈 高 |
| D | **多模型集成 (Ensemble)** | 动量/反转/事件/微观结构各自投票 | 不相关 alpha 叠加提升 Sharpe | 中 | 🥈 高 |
| E | **因子择时 (Factor Timing)** | 不同时期切换主导因子 | 单因子会轮动失效, 择时延长有效寿命 | 中 | 🥉 中 |
| F | **统计套利/配对交易** | 协整对做多低估/做空高估 | 市场中性, 剥离系统性风险 | 高 | 🥉 中 |
| G | **盘口微观结构** | Level-2 大单流向、买卖压力 | 比日线提前数周期捕捉资金意图 | 高 | 观察 |
| H | **另类数据** | 舆情热度、搜索指数、供应链 | 信息优势源于数据稀缺 | 高 | 观察 |

### 6.2 最值得优先做的三件事（超越 plan.md 第二章）

#### 6.2.1 【最高】波动率目标化仓位 (Volatility Targeting)

**为什么最先做**: 证据最强、实现最简、收益风险比改善最直接。回撤改善直接转化为复利优势——**少亏 20% 需要多赚 25% 才能追回**。

**核心思想**: 设定目标组合波动率, 实际波动率高于目标时按比例降仓。

```python
def volatility_target_scale(
    realized_vol: float,      # 近 20 日组合已实现波动率(年化%)
    target_vol: float = 15.0, # 目标波动率
    max_scale: float = 1.0,
    min_scale: float = 0.2,
) -> float:
    """波动率目标化仓位缩放因子。"""
    if realized_vol <= 1e-6:
        return max_scale
    scale = target_vol / realized_vol
    return round(max(min_scale, min(max_scale, scale)), 4)
```

**数据来源**: 用大盘指数或候选组合近 20 日日收益标准差估算 `realized_vol`。
**集成点**: 与 2.3.3 动态仓位缩放相乘, 作为组合级总仓位闸门。
**当前落地**: `portfolio.py` 先用候选池 `volatility_20d` 均值估算组合波动率, 缺失时回退 `amplitude`; `gross_exposure = regime_factor × drawdown_factor × volatility_factor`。summary 输出 `volatility_factor`, `portfolio_volatility_pct`, `target_volatility_pct` 和降仓原因。
**配置**: `ENABLE_VOLATILITY_TARGETING`, `PORTFOLIO_TARGET_VOLATILITY_PCT`, `PORTFOLIO_VOL_SCALE_MIN`, `PORTFOLIO_VOL_SCALE_MAX`。

#### 6.2.2 【最高】组合层 Max-Sharpe 优化

**为什么重要**: 当前系统"逐只排序取 Top-K", **完全忽略个股间相关性**。若 Top-K 全是同板块高相关股, 名义分散实则集中, 回撤会放大。

**核心思想**: 在已通过打分/门控的候选池上, 做一层组合优化, 在相关性约束下最大化组合 Sharpe。

```python
import numpy as np

def max_sharpe_weights(
    expected_returns: np.ndarray,   # 各候选股预期收益(可用校准概率×平均涨幅估)
    cov_matrix: np.ndarray,         # 近 60 日收益协方差矩阵
    max_weight: float = 0.25,       # 单票上限
    long_only: bool = True,
) -> np.ndarray:
    """在单票权重上限约束下近似求解 Max-Sharpe 组合权重。"""
    n = len(expected_returns)
    if n == 0:
        return np.array([])
    inv_cov = np.linalg.pinv(cov_matrix + np.eye(n) * 1e-6)
    raw = inv_cov @ expected_returns
    if long_only:
        raw = np.clip(raw, 0, None)
    if raw.sum() <= 0:
        return np.ones(n) / n
    weights = raw / raw.sum()
    # 施加单票上限后归一化(简单迭代)
    for _ in range(10):
        over = weights > max_weight
        if not over.any():
            break
        excess = (weights[over] - max_weight).sum()
        weights[over] = max_weight
        under = ~over
        if under.any():
            weights[under] += excess * weights[under] / weights[under].sum()
    return weights
```

**集成点**: 作为打分/门控之后的"组合构建层", 输出的是**权重**而非排名, 直接对接仓位分配。
**当前落地**: `portfolio.py` 基础版不引入 numpy/scipy, 采用风险预算近似:
- 原始权重仍由置信度/风险/波动率生成。
- 若收益模型已通过门控且 `model_confidence=ready`, 才允许用 `expected_return_net`、`p_win` 或 `calibrated_probability` 对原始权重做预期收益/胜率倾斜。
- 若收益模型仍是 `low`/`shadow`, 组合优化只能使用单票 cap、主题 cap、相关性组 cap 和波动率目标化, 不得因影子收益字段加仓。
- 在单票 cap、主题 cap 之外, 增加 `correlation_group` / `risk_group` / `industry` / `theme` 相关性组 cap。
- summary 输出 `correlation_exposure`, `correlation_group_cap_pct`, `portfolio_optimization_enabled`。
完整协方差 Max-Sharpe 留到后续有稳定历史收益矩阵时升级。
**预期收益**: 同等胜率下回撤更小 → Sharpe 提升 → 复利优势。
**配置**: `ENABLE_PORTFOLIO_OPTIMIZATION`, `PORTFOLIO_CORRELATION_GROUP_CAP`。

#### 6.2.3 【高】催化剂/事件 alpha 独立成源

**现状**: 系统已有 DeepSeek 事件评分 (`sentiment.py` / `deepseek_client.py`), 但**只用于对量价候选做重排序**, 事件本身未成为独立信号源。

**核心思想**: 把"可验证催化剂"(业绩预告、政策受益、大额订单、重组、回购) 独立成一个事件驱动策略, 与量价策略并行产出候选, 再由 6.2.4 集成层合并。

**DeepSeek 的正确角色**:
- **结构化抽取**: 从公告、新闻、研报摘要、互动问答中抽取事件类型、兑现窗口、来源可信度和风险点。
- **语义去重**: 合并同一事件的多条重复文本, 防止同一催化剂被重复加分。
- **新旧催化识别**: 区分"新增信息"和"市场已充分反映的信息"; 已发酵过热的事件应降权或转为风险提示。
- **风险反证**: 抽取监管问询、业绩不及预期、减持、诉讼、退市风险、涨停不可买等负面信号, 作为 skip/reduce 候选。
- **解释归因**: 生成可审计的推荐原因和风险原因, 但不直接给最终买卖指令。

**不得使用 DeepSeek 的方式**:
- 不让 DeepSeek 直接输出"买入/卖出/目标价"并绕过量价模型。
- 不把 LLM 主观置信度等同于胜率或预期收益。
- 不使用信号时间之后才发布的新闻、公告或收盘信息回填当时推荐。
- 不在没有 OOS 证明前, 让 DeepSeek 分数替代 `score` / `rank_score`。

```python
def event_alpha_score(events: list) -> dict:
    """对个股的催化剂事件打分, 独立于量价。"""
    weight_map = {
        "earnings_preannounce_up": 30,  # 业绩预增
        "policy_beneficiary": 20,       # 政策受益
        "major_order": 18,              # 大额订单
        "buyback": 12,                  # 回购
        "restructuring": 15,            # 重组
        "institutional_buy": 14,        # 机构调研/增持
    }
    score = 50.0
    hits = []
    for ev in events:
        etype = ev.get("type")
        confidence = float(ev.get("confidence", 0.5))  # DeepSeek 给出的置信度
        horizon_match = float(ev.get("horizon_match", 1.0))  # 时间窗匹配度
        contrib = weight_map.get(etype, 0) * confidence * horizon_match
        score += contrib
        if contrib > 0:
            hits.append({"type": etype, "contrib": round(contrib, 2)})
    return {"event_score": round(min(100.0, score), 2), "hits": hits}
```

**关键**: 事件时间窗要与策略周期匹配 (明日优先偏好"次日兑现"事件, 2-5日偏好"数日发酵"事件——系统已有此逻辑, 需强化为独立评分)。

**当前落地**:
- 新增 `stock_analyzer/event_alpha.py`, 提供 `event_alpha_score(events, strategy_name)`、`row_event_alpha_events(row)`、`attach_event_alpha(rows, strategy_name)`。
- 支持结构化事件列表, 也能从行上的 `deepseek_event_type`、`deepseek_catalyst_score`、`deepseek_time_sensitivity` 等字段提取事件。
- 输出 `event_alpha_score`、`event_alpha_active`、`hits` 和贡献拆分。
- 当前仍是独立 alpha 分, 不接管量价候选生成; 后续由 6.2.4 ensemble 或独立事件候选源接入。

**DeepSeek 是否提高收益的判定方法**:
- 建立三组 shadow 对照: `base_score Top-K`、`base_score + DeepSeek风险过滤`、`base_score + event_alpha/ensemble`。
- 每组使用完全相同的信号时间、候选池、成本模型和持有周期。
- 统计 DeepSeek 的边际贡献: `delta_avg_return_net`、`delta_sortino`、`delta_max_drawdown`、`delta_skip_loss_avoidance`、`delta_turnover_cost`。
- 若 DeepSeek 只提高解释质量但不提升 OOS 净收益或降低回撤, 则只能保留为解释层。
- 若 DeepSeek 降低收益或提高跳过优质标的比例, 立即关闭其排序/过滤影响, 只保留审计字段。

**配置**: `ENABLE_EVENT_ALPHA`, `EVENT_ALPHA_MIN_SCORE`。

#### 6.2.4 【高】多模型集成 (Ensemble)

**核心思想**: 让互不相关的 alpha 源各自独立打分, 再加权投票。不相关信号叠加能在**不增加单一风险**的前提下提升 Sharpe。

```python
def ensemble_score(model_scores: dict, model_weights: dict) -> dict:
    """
    集成多个独立模型的打分。
    model_scores: {"momentum": 72, "reversal": 45, "event": 80, "microstructure": 60}
    model_weights: {"momentum": 0.35, "reversal": 0.15, "event": 0.30, "microstructure": 0.20}
    """
    total_w = sum(model_weights.values()) or 1.0
    blended = sum(
        model_scores.get(k, 50.0) * w for k, w in model_weights.items()
    ) / total_w
    # 一致性: 各模型分歧越小, 置信越高
    values = [model_scores.get(k, 50.0) for k in model_weights]
    dispersion = (max(values) - min(values)) if values else 0.0
    agreement = round(max(0.0, 1.0 - dispersion / 100.0), 4)
    return {
        "ensemble_score": round(blended, 2),
        "agreement": agreement,   # 可作为置信度/仓位调节输入
    }
```

**模型权重的确定**: 用验证库真实样本, 对各子模型做 walk-forward 回归求最优组合权重 (类似 2.2.1 的思路, 但特征是各模型输出而非各因子)。

**当前落地**:
- 新增 `stock_analyzer/ensemble.py`, 提供 `ensemble_score(model_scores, model_weights)`、`row_model_scores(row)`、`attach_ensemble_score(rows)`。
- 默认融合 `price_volume`、`expected_return`、`event`、`probability`、`meta` 五类 shadow/独立分数。
- 输出 `ensemble_score`、`agreement`、`dispersion`、`model_scores`、`model_weights`。
- 默认不改变排序; 后续需 walk-forward 验证 ensemble Top-K 优于单模型后再接入排序。

**配置**: `ENABLE_ENSEMBLE`, `ENSEMBLE_MODEL_WEIGHTS`。

### 6.3 三层升级路径的关系

```
第一层 (第二章): 优化现有量价模型 —— 地基
    ├─ 平滑惩罚 / Sortino / 时间衰减
    ├─ 因子交互 / 概率校准 / 分状态权重
    └─ Meta-Labeling / 新因子 / 动态仓位

第二层 (6.2.1-6.2.2): 组合与风险层 —— 立竿见影
    ├─ 波动率目标化仓位  ← 最先做
    └─ 组合层 Max-Sharpe 优化  ← 最先做

第三层 (6.2.3-6.2.4 及 6.1 其余): 新 alpha 源 —— 突破天花板
    ├─ 事件 alpha 独立化
    ├─ 多模型集成
    └─ 因子择时 / 统计套利 / 微观结构 / 另类数据
```

**建议顺序**: 先做第一层地基 (第二章 2.1) → 立即插入第二层 6.2.1/6.2.2 (投入产出比最高) → 再逐步引入第三层新 alpha 源。

### 6.4 核心判断

> **没有"更优到保证收益"的逻辑, 只有"更优到提升风险调整收益"的逻辑。**

持续超额收益的三大真实来源:
1. **更多不相关的 alpha 源** (事件、套利、微观结构) —— 6.2.3 / 6.2.4 / 6.1
2. **更好的风险控制** (波动率目标化、组合优化、尾部管理) —— 6.2.1 / 6.2.2
3. **更长的策略有效期管理** (因子择时、动态退场、时间衰减) —— 6.1-E / 2.1.3 / 2.3.3

第二章解决了 (2) 和 (3) 的一部分地基, 但 (1) 必须引入本章的新 alpha 源才能突破当前纯量价框架的天花板。

---

### 6.5 数据与执行层的修正逻辑（对应 1.5 节新发现）

> 以下逻辑不产生新的 alpha, 但能**消除系统性高估**, 让回测指标更接近实盘,
> 从而让 6.1-6.2 的策略改进建立在可信的验证基础之上。优先级: **先于所有模型改进**。

#### 6.5.1 【先做】尾盘执行价滑点修正

**对应问题**: 1.5.2 尾盘入场执行价偏差。

**核心思想**: 在尾盘集合竞价场景下, 实际成交价相对信号价有系统性偏差, 应额外加收执行摩擦成本。

```python
def tail_auction_slippage(
    signal_price: float,
    daily_turnover: float,        # 当日成交额
    order_amount: float,          # 计划下单金额
    base_slippage: float = 0.0,   # 原有滑点
) -> float:
    """尾盘集合竞价的额外执行摩擦。

    逻辑: 成交额越小、下单金额越大, 偏离越严重。
    近似: 尾盘集合竞价流动性约为全日的 3%-8%, 此处取 5% 估算有效流动性。
    """
    if signal_price <= 0 or daily_turnover <= 0:
        return base_slippage
    effective_liquidity = daily_turnover * 0.05  # 尾盘可用流动性估算
    if effective_liquidity <= 0:
        return base_slippage + 0.5  # 极端不流动性兜底
    participation = order_amount / effective_liquidity
    # 参与率越高, 额外摩擦越大; 封顶 0.8%
    extra = min(0.8, participation * 100 * 0.02)
    return round(base_slippage + extra, 4)
```

**集成点**: 替换/增强 `strategy_validation._execution_cost_pct()` 的滑点计算, 并在回测 `backtest.py` 中同步使用。
**配置**: `ENABLE_TAIL_AUCTION_SLIPPAGE`, `TAIL_AUCTION_LIQUIDITY_RATIO = 0.05`。

#### 6.5.2 【先做】平方根市场冲击模型

**对应问题**: 1.5.3 市场冲击未建模、资金容量无上限。

**核心思想**: 用经典平方根模型估算自身下单对价格的冲击, 让策略有明确的资金容量边界。

```python
import math

def market_impact_cost(
    order_amount: float,        # 下单金额(元)
    adv: float,                 # 日均成交额(20日平均)
    coefficient: float = 0.1,   # 冲击系数, 经验值 0.05-0.15
) -> float:
    """平方根市场冲击成本(%)。

    模型: impact% = coefficient * sqrt(order_amount / adv) * 100
    当 order_amount/adv = 1% 时, impact ≈ coefficient%; 
    当 order_amount/adv = 10% 时, impact ≈ coefficient * sqrt(0.1) ≈ 3.16%。
    """
    if adv <= 0 or order_amount <= 0:
        return 0.0
    participation = order_amount / adv
    if participation <= 0:
        return 0.0
    impact_pct = coefficient * math.sqrt(participation) * 100
    return round(min(impact_pct, 5.0), 4)  # 封顶 5%


def strategy_capacity_check(
    candidates: list,            # 候选股列表
    total_capital: float,        # 总资金
    max_impact_pct: float = 1.0, # 单票最大可接受冲击
) -> dict:
    """检查策略在给定资金下的容量, 返回可投资股票与超额资金。"""
    investable = []
    overflow = []
    per_stock_capital = total_capital / max(1, len(candidates))
    for stock in candidates:
        adv = stock.get("adv_20d", stock.get("turnover", 0))
        impact = market_impact_cost(per_stock_capital, adv)
        if impact <= max_impact_pct:
            investable.append({**stock, "impact_pct": impact})
        else:
            overflow.append({**stock, "impact_pct": impact, "reason": "市场冲击超限"})
    return {
        "investable": investable,
        "overflow": overflow,
        "capacity_ok": len(overflow) == 0,
    }
```

**集成点**: 在仓位分配阶段调用 `strategy_capacity_check()`, 对冲击超限的股票降权或剔除; 回测成本中加入 `market_impact_cost`。
**配置**: `ENABLE_MARKET_IMPACT`, `MARKET_IMPACT_COEFFICIENT = 0.1`, `MAX_ACCEPTABLE_IMPACT_PCT = 1.0`。

#### 6.5.3 【中】主题集中度联动市场状态

**对应问题**: 1.5.4 主题集中度未联动市场状态。

```python
def regime_aware_theme_cap(market_regime: dict, base_cap: int = 3) -> int:
    """按市场状态动态调整单主题集中度上限。"""
    level = market_regime.get("level", "balanced")
    if level == "risk_off":
        return max(1, base_cap - 1)   # 防御期更分散
    elif level == "risk_on":
        return base_cap               # 进攻期维持
    else:
        return base_cap
```

**集成点**: 替换 `recommendation_runtime_support.py:346` 中固定 `theme_cap = 3`。
**配置**: `ENABLE_REGIME_THEME_CAP`。

#### 6.5.4 【先做】幸存者偏差修正与退市样本保留

**对应问题**: 1.5.1 幸存者偏差完全未处理 (最严重)。

**核心思想**: 退市/长期停牌股票的最后一个可得收盘价作为退出价, 不允许其从验证样本中"消失"。

```python
def backfill_delisted_outcome(
    signal_row: dict,
    history: pd.DataFrame,
    default_loss_pct: float = -30.0,  # 无数据时的兜底假设亏损
) -> dict:
    """对退市/停牌信号回填结果, 防止幸存者偏差。

    若信号后历史数据终止, 用最后一个可得收盘价作为退出价;
    若完全无后续数据, 按默认亏损估算(保守)。
    """
    signal_date = str(signal_row.get("signal_date", ""))
    signal_price = coerce_number(signal_row.get("price_at_signal"))
    post = history[history["trade_date"].astype(str) > signal_date] if not history.empty else pd.DataFrame()
    if post.empty or signal_price <= 0:
        return {
            "outcome_updated_at": "",
            "exit_price": None,
            "next_close_return": default_loss_pct,
            "skip_reason": "delisted_or_no_data",
            "survivorship_corrected": True,
        }
    last_row = post.iloc[-1]
    last_price = coerce_number(last_row.get("price") or last_row.get("close"))
    if last_price <= 0:
        return {
            "outcome_updated_at": str(last_row.get("trade_date", "")),
            "exit_price": None,
            "next_close_return": default_loss_pct,
            "skip_reason": "delisted_zero_price",
            "survivorship_corrected": True,
        }
    ret = (last_price / signal_price - 1) * 100
    return {
        "outcome_updated_at": str(last_row.get("trade_date", "")),
        "exit_price": last_price,
        "next_close_return": round(ret, 4),
        "skip_reason": "delisted_liquidation_exit",
        "survivorship_corrected": True,
    }
```

**配套**: 在 `StrategyValidationStore.metrics()` 输出中新增字段:
- `survivorship_corrected_count`: 被退市修正的样本数
- `win_rate_all` vs `win_rate_survivors`: 全样本 vs 存续样本胜率, 两者差距即幸存者偏差幅度

**预期**: 修正后真实胜率/收益会**下降**, 但这是更诚实的数据; 只有修正后仍能通过门控的策略才值得信任。
**配置**: `ENABLE_SURVIVORSHIP_CORRECTION`, `DELISTED_DEFAULT_LOSS_PCT = -30.0`。

### 6.6 统计与压力测试修正（对应 1.6 节新发现）

> 以下逻辑不产生新的 alpha, 但能**降低假发现率、量化尾部风险**, 让上线决策更保守、更可信。

#### 6.6.1 【中】情景压力测试

**对应问题**: 1.6.1 缺乏黑天鹅/压力测试机制。

**核心思想**: 主动识别历史上的极端交易日, 计算策略在这些日子的损失, 作为上线/仓位决策的额外输入。

```python
import pandas as pd

STRESS_SCENARIOS = [
    {"name": "2015股灾", "dates": [("2015-06-19", "2015-08-26")], "index_drop": -0.43},
    {"name": "2018贸易战", "dates": [("2018-02-26", "2018-12-31")], "index_drop": -0.31},
    {"name": "2020疫情暴跌", "dates": [("2020-01-20", "2020-03-19")], "index_drop": -0.15},
    {"name": "2024微盘崩盘", "dates": [("2024-01-29", "2024-02-05")], "index_drop": -0.25},
]

def stress_test_strategy(
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    scenarios: list = None,
) -> dict:
    """计算策略在各压力情景下的表现。"""
    scenarios = scenarios or STRESS_SCENARIOS
    results = []
    merged = signals.merge(outcomes, on="signal_id", how="inner")
    for sc in scenarios:
        mask = pd.Series(False, index=merged.index)
        for start, end in sc["dates"]:
            mask |= (merged["signal_date"] >= start) & (merged["signal_date"] <= end)
        subset = merged[mask]
        if subset.empty:
            continue
        results.append({
            "scenario": sc["name"],
            "sample_count": len(subset),
            "win_rate": round((subset["signal_next_close_return"] > 0).mean() * 100, 2),
            "avg_return": round(subset["signal_next_close_return"].mean(), 4),
            "max_drawdown": round(subset["signal_next_close_return"].min(), 4),
            "total_return": round(subset["signal_next_close_return"].sum(), 4),
        })
    return {"scenarios": results, "worst_scenario": min(results, key=lambda x: x["total_return"]) if results else None}
```

**集成点**: 在 `strategy_health.py` 中, 除现有胜率/收益/回撤阈值外, 增加"压力测试累计亏损不超过基准"的软约束; 在 UI 展示各策略的压力测试结果。

**当前落地**:
- 新增 `stock_analyzer/stress_scenarios.py`, 提供 `load_stress_scenarios()` 和 `stress_test_samples()`。
- 输入验证样本与情景日期窗口, 输出每个情景的样本数、胜率、平均收益、最大单笔损失、累计收益和 `worst_scenario`。
- 当前不接入硬门控, 后续接到 `strategy_health.py` 和验证看板。

**配置**: `ENABLE_STRESS_TEST`, `STRESS_TEST_SCENARIOS_PATH`。

#### 6.6.2 【中】多重假设检验校正

**对应问题**: 1.6.2 缺乏多重假设检验校正。

**核心思想**: 当校准过程尝试了 N 组参数, 只有校正后的显著性水平才可信。A股数据噪声高, 假发现风险尤其大。

```python
def benjamini_hochberg_fdr(p_values: list, q: float = 0.1) -> dict:
    """Benjamini-Hochberg FDR 控制。

    返回哪些 p 值在 q 水平下显著, 及整体 reject 比例。
    """
    if not p_values:
        return {"rejected": [], "rejected_count": 0}
    sorted_p = sorted((p, i) for i, p in enumerate(p_values) if p is not None and 0 <= p <= 1)
    n = len(sorted_p)
    rejected = []
    for k, (p, i) in enumerate(sorted_p, start=1):
        threshold = q * k / n
        if p <= threshold:
            rejected.append(i)
    return {"rejected": rejected, "rejected_count": len(rejected)}


def calibrate_with_fdr_guard(
    candidate_configs: list,
    evaluate_fn,
    q: float = 0.1,
) -> dict:
    """在坐标下降/Ridge校准中增加 FDR 门控。

    1. 对每个候选配置做 walk-forward OOS 评估, 得到 p_value(原假设: 该配置无 alpha)
    2. 用 BH-FDR 选出显著配置
    3. 只在显著配置中选择最优
    """
    evaluated = []
    for cfg in candidate_configs:
        metrics = evaluate_fn(cfg)
        evaluated.append({"config": cfg, "metrics": metrics, "p_value": metrics.get("p_value", 1.0)})
    p_values = [e["p_value"] for e in evaluated]
    fdr_result = benjamini_hochberg_fdr(p_values, q)
    significant = [evaluated[i] for i in fdr_result["rejected"]]
    if not significant:
        return {"selected": None, "reason": "FDR 校正后无显著配置, 维持默认权重", "fdr_result": fdr_result}
    best = max(significant, key=lambda x: x["metrics"].get("objective", -1e9))
    return {"selected": best["config"], "fdr_result": fdr_result, "metrics": best["metrics"]}
```

**p_value 计算**: 可用 bootstrap 或 permutation test 计算"该配置收益是否显著为正"。

**当前落地**:
- `calibrate.py` 已新增 `benjamini_hochberg_fdr()` 与 `calibrate_with_fdr_guard()`。
- 2.2.1/2.2.3/2.3.1 的 shadow evaluator 已输出 sign-test/FDR guard 状态。
- 2.2.0 收益目标化排序在替代旧排序前, 必须补齐同级别的 FDR/sign-test guard 和 CI 下界检查。
- 坐标下降 `_fit_weights()` 的候选扫描尚未整体替换为 BH-FDR 选择器, 后续可在候选配置枚举更完整后接入。

**集成点**: 在 `calibrate.py` 的 `_fit_weights()` / coordinate descent 外层加 FDR 门控; 当 2.2.1 扩展为 Ridge/多项式特征时, 该门控尤为重要。
**配置**: `ENABLE_CALIBRATE_FDR`, `CALIBRATE_FDR_Q = 0.1`。

---

### 6.7 改进实施的正确顺序（修正版）

由于 6.5 的数据/执行层修正确保了验证指标的可信度, **必须先于模型改进完成**。本轮文档计划将可用能力分成"先开保守修正"和"后开预测模型"两类:

```
第 0 步 (前置, 必做) ── 第一批默认开启: 保守修正 + 风控约束
    ├─ 6.5.4 幸存者偏差修正 (最严重, 否则后续所有验证都失真)
    ├─ 6.5.1 尾盘执行价滑点
    ├─ 6.5.2 市场冲击模型
    ├─ 6.5.3 主题集中度联动
    ├─ 2.1 地基改进 (平滑惩罚/Sortino/时间衰减)
    └─ 2.3.3/6.2.1/6.2.2 动态仓位 + 波动率目标化 + 相关性约束
            ↓ 修正后重新基线化所有策略的真实 metrics
第 1 步 ── 第二批开启: 解释型 shadow 输出
    ├─ 2.2.0 收益模型 shadow 字段
    └─ 2.2.2 概率校准解释
            ↓ 不改变排序、不改变组合权重、不改变执行
第 2 步 ── 第三批灰度: 预测模型生产化
    ├─ 2.2.0 rank_score 替代旧 score 排序
    ├─ 2.2.1 因子交互
    ├─ 2.2.3 分市场状态权重
    └─ 2.3.1 Meta-Labeling enforce
第 3 步 ── 6.2.3/6.2.4 新 alpha 源 (事件/集成)
            │
并行约束 ── 6.6 统计与压力测试修正 (FDR/压力情景)
            ↓ 降低假发现率, 量化尾部风险
```

**关键判断**: 若跳过第 0 步直接做模型改进, 可能在"被高估的指标"上优化, 导致 OOS 改善是虚假的——这是比过拟合更隐蔽的陷阱。
**另一关键判断**: 6.6 的 FDR 门控应在每次校准 (2.2.0/2.2.1/2.3.1) 中同步启用, 否则参数空间扩大后假发现会激增。

---

## 七、根本原则重申

> 无论采用何种打分机制, A 股短线推荐**不可能保证收益**。

原因:
1. **非平稳性**: 市场结构会变, 有效的因子会衰减
2. **不可预测事件**: 监管政策、公司公告、外围暴跌等无法建模
3. **过拟合风险**: 越复杂的模型越容易对历史数据过拟合
4. **执行偏差**: 模型推荐与实盘之间存在滑点、流动性冲击

本改进计划的目标不是"保证收益", 而是:
- 提升评分的**稳健性** (减少过拟合)
- 增强结果的**可解释性** (概率校准)
- 加速对市场变化的**适应性** (时间衰减、动态仓位)
- 更好地管理**尾部风险** (Sortino、Meta-Labeling)

系统的设计哲学保持不变: **用持续验证闭环替代"保证收益"的幻觉**。

---

## 八、工程级优化计划（交付拆分）

本节把前文策略优化拆成工程可交付项。优先级规则: **先修正验证可信度, 再优化排序, 再做组合与新 alpha 源, 最后扩大自动化和前端解释**。

### 8.1 P0 数据与验证地基

| 目标 | 主要模块 | 交付物 | 验收标准 |
|------|----------|--------|----------|
| 修正收益口径与执行偏差 | `strategy_validation.py`, `risk_rules.py`, `backtest.py` | 统一 `tomorrow_picks` 次日开盘入场、`swing_picks` 动态退出和滑点成本 | 信号保存、回填、页面展示使用同一主收益字段 |
| 处理幸存者偏差 | `strategy_validation.py`, `daily_data.py` | 退市/长期停牌样本保留与保守回填 | metrics 同时输出全样本与存续样本指标 |
| 补齐回填完整性 | `daily_job.py`, `validation_runtime_support.py` | 未成熟/缺失结果自动补齐和异常统计 | 未回填样本数可解释, 不静默消失 |
| 修复门控绕行 | `app.py`, `app_support.py` | 所有快照兜底路径统一重跑验证门控 | 旧缓存不能绕过 `execution_allowed=false` |

### 8.2 P1 评分层升级

| 目标 | 主要模块 | 交付物 | 验收标准 |
|------|----------|--------|----------|
| 平滑风险扣分 | `scoring.py`, `config.py` | `_smooth_penalty()` 与开关 `USE_SMOOTH_PENALTY` | 边界样本排序不再因 0.1% 指标差异跳变 |
| 改造校准目标 | `calibrate.py` | Sortino、下行分位、时间衰减目标函数 | walk-forward OOS 不低于 baseline |
| 收益目标化排序 | `expected_return_model.py`, `scoring.py`, `calibrate.py` | `expected_return_net`, `p_win`, `downside_p10`, `rank_score` | 60 个真实交易日后才允许灰度; OOS/FDR 通过才替代旧排序 |
| 概率解释 | `probability_calibration.py`, 前端推荐表 | 分数段历史胜率/概率标签 | 页面明确区分排序分、概率和预期收益 |

### 8.3 P2 组合与仓位层

| 目标 | 主要模块 | 交付物 | 验收标准 |
|------|----------|--------|----------|
| 波动率目标化 | `portfolio.py`, `strategy_health.py` | 组合级总仓位缩放；基础版已接入候选波动率估算 | 高波动期自动降仓, 回撤低于 baseline |
| 组合优化 | `portfolio.py` | 风险预算权重基础版；相关性组 cap；收益模型 ready 后才允许预期收益倾斜 | 单票、单主题、相关性约束生效; 低置信收益字段不影响权重 |
| 动态仓位缩放 | `app_support.py`, `portfolio.py` | `position_scale` 连续仓位因子；硬失败仍阻断 | 边缘策略降仓而不是断崖式全停 |
| 市场冲击成本 | `portfolio.py`, `strategy_validation.py` | 平方根冲击模型与资金容量检查 | 大资金/低成交额候选自动降权或剔除 |

### 8.4 P3 模型与新 alpha 源

| 目标 | 主要模块 | 交付物 | 验收标准 |
|------|----------|--------|----------|
| 因子交互 | `calibrate.py`, `scoring.py` | 基础版无依赖 shadow evaluator; 后续 Ridge/Huber + 二阶交互项 | 多数 OOS fold 改善且 FDR 通过后才允许启用生产排序 |
| Meta-Labeling | 新 `meta_labeling.py`, `app_support.py` | 主模型置信度与跳过/降仓决策 | 使用元模型后主周期净收益或回撤改善 |
| 事件 alpha 独立化 | `sentiment.py`, `deepseek_client.py`, 新 `event_alpha.py` | 催化剂事件独立打分 | 事件信号独立归因, 不能只靠 LLM 主观判断 |
| Ensemble | 新 `ensemble.py`, `calibrate.py` | 多 alpha 源加权投票 | 单模型失效时集成模型回撤更低 |

### 8.5 P4 前端、运维与回归测试

| 目标 | 主要模块 | 交付物 | 验收标准 |
|------|----------|--------|----------|
| 推荐表解释升级 | `static/recommendation-renderers.js`, `templates/index.html` | 展示 `rank_score`、预期净收益、胜率概率、置信度和原因 | 用户能看出"高分"与"可执行"的区别 |
| 验证看板升级 | `static/validation-*`, `strategy_validation.py` | 全样本/存续样本、OOS、FDR、压力测试结果 | 任何策略上线前能看到完整证据链 |
| 配置与回退 | `config.py`, `.runtime/weights.json` | 所有新机制都有环境变量开关 | 单项功能可独立关闭并回到当前行为 |
| 测试矩阵 | `tests/` | 单元、回归、快照、校准门控测试 | 每个阶段合入前测试通过, 关键收益口径有断言 |

### 8.6 推荐实施节奏

1. **文档阶段（当前）**: 只更新本文档, 明确第一批开启项、shadow 边界、生产灰度门槛和回退规则。
2. **代码阶段 A**: 默认开启 P0/P1 中已实现且偏保守的能力: 幸存者偏差、尾盘滑点、市场冲击、主题 cap、平滑扣分、Sortino、时间衰减、FDR、压力测试、动态仓位、波动率目标化和相关性 cap。
3. **代码阶段 B**: 重新回填并生成修正后 baseline; 所有旧指标标记为旧口径, 不再用于证明新策略有效。
4. **代码阶段 C**: 开启收益模型、概率校准和 Meta-Labeling 的 shadow 输出; 明确 `model_confidence != ready` 时不参与排序、组合权重或执行。
5. **代码阶段 D**: 若真实样本数、OOS、FDR、CI 下界全部通过, 灰度让 `rank_score` 替代旧排序, 再逐项评估交互项、分状态权重和 Meta enforce。
6. **代码阶段 E**: 逐步引入 P3 新 alpha 源和模型集成; 每个 alpha 必须单独归因、单独门控。

任何阶段只要真实前瞻指标、执行跳过率或回撤明显恶化, 立即关闭对应开关并回退到上一阶段 baseline。

---

## 九、剩余优化空间

当前计划已经覆盖评分目标、验证口径、仓位风控和新 alpha 源, 但仍有进一步提升空间。以下优化都必须以"扣成本后风险调整收益"为验收目标, 不以单纯胜率或展示效果为目标。

### 9.1 排序目标继续收敛到可交易效用

当前 `rank_score` 已从强弱分转向期望净收益, 但后续应进一步升级为可交易效用:

```text
trade_utility =
    E[primary_return_net]
    - lambda_downside * expected_shortfall_or_MAE
    - lambda_cost * execution_cost
    - lambda_turnover * turnover_cost
    - lambda_uncertainty * model_uncertainty
    - lambda_capacity * market_impact
```

验收标准:
- Top-K `trade_utility` 的 OOS 净收益、Sortino、最大回撤均优于旧 `score`。
- 同等收益下, 换手率、滑点、冲击成本更低。
- `trade_utility` 不得用未 ready 的 `expected_return_net/p_win` 加仓或重排。

### 9.2 验证方法升级: 防止时间泄漏和样本重叠

短线策略标签存在时间重叠, 尤其是 2-5 日持有。普通 walk-forward 仍可能让相邻训练/测试样本共享行情路径。

优化项:
- 引入 purged walk-forward: 测试窗口前后增加 embargo 间隔, 避免持仓区间重叠。
- 所有特征按 `signal_time` 可见性检查, 不允许使用信号后才知道的收盘、板块、资金或 LLM 信息。
- 对 `tomorrow_picks`、`swing_picks` 分别维护独立验证切分, 不混用样本。

验收标准:
- purged OOS 下仍优于 baseline。
- 去掉任一高风险特征后, 结果不应崩塌; 若崩塌则说明模型依赖单点特征。

### 9.3 因子质量治理: 只保留能贡献排序能力的因子

新增因子不等于新增收益。每个因子必须证明它能提升横截面排序。

优化项:
- 对每个因子计算 RankIC、Top-K lift、分桶单调性和覆盖率。
- 对行业、市值、市场状态做中性化或分组评估, 避免只是在押注单一风格。
- 对高度相关因子做去重或正交化, 防止同一信号被重复计分。
- 建立因子退役规则: 连续多个窗口 RankIC 接近 0 或反向时降权/移除。

验收标准:
- 新因子入主模型前, 必须在至少两个独立时间窗口提升 Top-K 净收益或降低回撤。
- 因子覆盖率不足时只能作为解释字段, 不得进入生产排序。

### 9.4 标签与执行继续细化

收益标签越接近真实成交, 评分越有意义。

优化项:
- `tomorrow_picks`: 继续使用次日开盘入场口径, 同时记录高开跳过、涨停不可买、开盘滑点和开盘后快速回撤。
- `swing_picks`: 将 `exit_return` 拆成最大不利波动(MAE)、最大有利波动(MFE)、实际退出收益和退出原因。
- 对止损/止盈模拟加入跳空、跌停不可卖、日内先后顺序不确定性的保守处理。
- 对不同资金规模输出容量曲线: 资金越大, 预期净收益应扣除更高冲击成本。

验收标准:
- 标签字段、页面展示、校准目标、组合权重使用同一净收益口径。
- 成本敏感性测试中, 交易成本上调后策略不应从显著盈利瞬间变成显著亏损。

### 9.5 组合层从"约束"升级为"边际收益/风险分配"

当前组合层以 cap 和风控为主, 后续可在收益模型 ready 后升级为边际贡献分配。

优化项:
- 权重由 `expected_return_net / downside_risk / correlation / capacity` 共同决定。
- 加入 turnover budget, 防止每天大幅换仓吞噬收益。
- 对同主题高相关候选只保留边际贡献最高者, 其余降为备选观察。
- 输出每只股票对组合预期收益、波动、回撤、相关性暴露的边际贡献。

验收标准:
- 组合级 OOS 净收益和 Sortino 优于等权 Top-K。
- 单票/主题/相关性约束触发时, 组合仍保留足够分散度和现金缓冲。

### 9.6 DeepSeek 与事件 alpha 需要结构化归因

DeepSeek 可以帮助提高荐股收益, 但前提是它提供了量价因子没有覆盖的独立信息, 并且该信息在真实前瞻 OOS 中表现为正的边际贡献。LLM 或事件分不能只给"看好/不看好"结论, 必须拆成可验证事件。

优化项:
- 事件字段标准化: 事件类型、强度、兑现窗口、来源可信度、是否已被价格反映。
- 区分"新增催化"和"已发酵过热"; 后者可能应降权而不是加分。
- 建立事件负样本: 高热度但未兑现、公告后冲高回落、监管利空等。
- LLM 只做结构化抽取和解释, 不直接绕过量价/验证门控。
- 建立 DeepSeek 边际贡献字段: `deepseek_event_alpha_delta`、`deepseek_risk_filter_delta`、`deepseek_skip_reason`、`deepseek_source_time`、`deepseek_evidence_hash`。
- 对 DeepSeek 输出做时间戳审计: 只有 `source_time <= signal_time` 的信息可进入当时评分。

推荐结合方式:
- **明日优先**: DeepSeek 只识别次日可能兑现的催化剂和负面风险; 若事件窗口超过 2-5 日, 不应给明日策略加分。
- **2-5日持有**: DeepSeek 可识别政策、订单、业绩、回购等多日发酵事件, 但要扣除已连续大涨和高热度过度反映。
- **盘中观察**: DeepSeek 只做风险提示和主题解释, 不形成可执行收益模型。
- **组合层**: DeepSeek 不直接加仓; 只有事件 alpha OOS 通过后, 才能作为 ensemble 的一个独立子模型权重。

验收标准:
- 事件 alpha 独立 Top-K 在 OOS 中有正净收益或能改善主模型回撤。
- DeepSeek 组合后的 Top-K 净收益、Sortino 或最大回撤至少一项显著优于不使用 DeepSeek 的 baseline, 且其他关键指标不得恶化。
- DeepSeek 风险过滤能证明避免了亏损样本, 而不是简单跳过所有高波动候选。
- DeepSeek 的错误类型可归因: 幻觉、信息滞后、事件已反映、来源低可信、周期不匹配。
- 事件信号失效时可单独关闭, 不影响量价主模型。

### 9.7 线上监控从结果统计升级为归因诊断

只看胜率和平均收益不足以判断为什么策略变好或变坏。

优化项:
- 每日输出收益归因: alpha 分数、风险扣分、成本、仓位缩放、组合约束分别贡献多少。
- 监控数据漂移: 因子分布、行业暴露、样本覆盖率、成交额、涨跌停比例。
- 监控执行漂移: 预测滑点 vs 实际滑点、跳过率、无法成交比例。
- 建立自动降级: 数据覆盖不足、成本异常、模型置信度下降时自动回到旧排序或观察池。

验收标准:
- 每次策略恶化都能定位到 alpha 失效、成本上升、风险暴露、数据异常或执行问题中的至少一类。
- 任何自动降级都必须在前端和验证看板中可解释。

### 9.8 最终优先级建议

| 优先级 | 优化项 | 原因 |
|--------|--------|------|
| P0 | purged walk-forward + 标签/执行一致性 | 没有可信验证, 后续所有收益判断都不可靠 |
| P0 | 成本敏感性和容量曲线 | 短线收益很容易被滑点和冲击成本吞噬 |
| P1 | 因子 RankIC/Top-K lift 治理 | 确保评分因子真正贡献排序能力 |
| P1 | trade_utility 排序目标 | 让评分直接服务扣成本风险调整收益 |
| P2 | 组合边际贡献权重 | 在收益模型 ready 后提升组合层效率 |
| P2 | 事件 alpha 结构化归因 | 拓展纯量价模型天花板, 但必须严格门控 |
| P3 | 线上归因诊断和自动降级 | 提升长期维护和失效发现能力 |

总体判断: 现有计划方向正确, 但后续优化重点应从"增加更多模型"转向"证明每个信号的边际收益贡献"。只有能提升净收益、降低成本、降低风险或提高验证真实性的机制, 才允许进入生产荐股和评分链路。

---

## 十、基于当前代码的可执行任务拆分

本节按当前代码中已经存在的模块、函数和配置开关拆任务。目标是把改进变成可以逐项开发、测试、灰度和回退的工程项。

### 10.0 当前开关分层: 能安全用的先打开, 收益型模型先验证

先打开的不是"所有复杂模型", 而是低过拟合、能降低风险或提高验证真实性的机制。凡是会直接改变荐股排序、仓位或执行动作的收益型模型, 必须先 shadow/OOS, 再灰度生产。

| 分层 | 当前开关/模块 | 建议状态 | 原因 |
|------|---------------|----------|------|
| 默认保持开启 | `USE_SMOOTH_PENALTY=1` | 开启 | 平滑风险扣分, 降低边界样本排序跳变 |
| 默认保持开启 | `ENABLE_PORTFOLIO_OPTIMIZATION=1`, `ENABLE_VOLATILITY_TARGETING=1` | 开启 | 控制单票/主题/相关性/波动风险, 不依赖收益模型加仓 |
| 默认保持开启 | `VALIDATION_AUTO_UPDATE_ENABLED=1`, `VALIDATION_AUTO_SNAPSHOT_ENABLED=1` | 开启 | 持续积累真实前瞻样本, 是后续收益判断的基础 |
| 默认保持开启 | `ENABLE_RISK_BLACKLIST=1`, `ENABLE_HISTORY_FACTORS=1` | 开启 | 风险过滤和历史因子覆盖属于基础质量控制 |
| 默认保持开启 | `CALIBRATE_USE_SORTINO=1`, `CALIBRATE_USE_TIME_DECAY=1` | 开启 | 校准时优先惩罚下行风险, 并弱化过旧样本 |
| 第一批灰度 | `ENABLE_TAIL_AUCTION_SLIPPAGE`, `ENABLE_MARKET_IMPACT`, `ENABLE_SURVIVORSHIP_CORRECTION` | 先并行输出新旧指标, 再切 baseline | 会让验证更真实, 但会改变历史口径, 需要避免新旧收益混算 |
| 只做 shadow | `ENABLE_EXPECTED_RETURN_RANKING`, `ENABLE_INTERACTION_TERMS`, `ENABLE_META_LABELING` | 默认不接管排序 | 直接影响荐股排序/动作, 必须等 OOS/FDR/CI 通过 |
| 只做 shadow | `ENABLE_EVENT_ALPHA`, `ENABLE_ENSEMBLE` | 默认不接管排序 | 事件和集成模型必须单独证明边际贡献 |
| DeepSeek 节约生产 | `ENABLE_DEEPSEEK_RUNTIME=1`, `DEEPSEEK_SCHEDULE_STRATEGIES=tomorrow_picks` | 只集中在明日优先和少量边界样本 | 让 token 花在最接近执行窗口、最可能产生边际收益的位置 |
| DeepSeek 暂缓扩大 | `ENABLE_DEEPSEEK_MARKET_GATE=0`, `DEEPSEEK_RERANK_DISABLED_STRATEGIES` | 保持可关闭、可按策略禁用 | 大盘 gate 和全策略 rerank 都可能放大 token 成本, 需先看单位收益 |

### 10.1 第一批: 不依赖 DeepSeek 的收益地基

| 步骤 | 当前代码入口 | 任务 | 验收标准 | 回退 |
|------|--------------|------|----------|------|
| A1 | `strategy_validation.py::_primary_return_config()`、`_compute_outcome()` | 确认 `tomorrow_picks=next_open_to_close`、`swing_picks=exit_return` 的主收益口径贯穿保存、回填、metrics 和页面 | 同一信号在 DB、metrics、前端展示中的主收益字段一致 | 保留旧字段但标记旧口径 |
| A2 | `strategy_validation.py::_execution_cost_pct()`、`tail_auction_slippage_pct()`、`market_impact_cost_pct()` | 将尾盘滑点和市场冲击纳入净收益标签, 并补成本敏感性测试 | 成本上调后策略表现变化可解释, 不出现静默高估 | 关闭 `ENABLE_TAIL_AUCTION_SLIPPAGE` / `ENABLE_MARKET_IMPACT` |
| A3 | `strategy_validation.py` survivorship 相关函数 | 开启并验证幸存者偏差修正, 输出全样本与存续样本指标 | `survivorship_corrected_count` 可见, 指标不再静默剔除坏样本 | 关闭 `ENABLE_SURVIVORSHIP_CORRECTION`, 但旧指标不得与新 baseline 混用 |
| A4 | `scoring.py::_smooth_penalty()` | 平滑风险扣分默认接入, 检查边界样本排序稳定性 | 10.9%/11.1% 等边界不再产生异常跳变 | `USE_SMOOTH_PENALTY=0` |
| A5 | `calibrate.py` 目标函数 | Sortino、下行分位、时间衰减作为校准目标默认启用 | OOS 不低于 baseline, 大亏样本权重更高 | 关闭对应校准开关 |
| A6 | `portfolio.py::build_portfolio()` | 先只启用单票 cap、主题 cap、相关性 cap、波动率目标化; 低置信收益字段不得加仓 | 组合暴露、现金、相关性组 cap 在 summary 中可解释 | 关闭 `ENABLE_PORTFOLIO_OPTIMIZATION` / `ENABLE_VOLATILITY_TARGETING` |

当前进展:
- 已新增 `strategy_validation.validation_baseline_config()`, 将主收益字段、净收益公式、成本模型组件、幸存者修正状态和 `baseline_id` 统一输出。
- `StrategyValidationStore.metrics()` 已返回 `validation_baseline` / `validation_baseline_id`; 推荐行 `similar_signal_stats` 也会透传该 baseline 信息。
- `strategy_outcomes` 已持久化 `validation_baseline_id` / `validation_baseline_json`; `signals_for_date()` 可查看每条 outcome 的验证口径。
- `metrics()` / `live_weight_samples()` / `signal_status_counts()` 已按当前 `validation_baseline_id` 过滤样本, 并输出 `current_baseline_outcome_count`、`legacy_baseline_outcome_count`、`excluded_baseline_mismatch_count` 等计数, 避免启用成本或幸存者修正后混用旧收益口径。
- `update_outcomes(..., only_incomplete=True)` 已把"当前 baseline 缺失"视为待回填, 可用于 A2/A3 灰度阶段补齐新口径。
- 已新增 `/api/strategy-validation/runtime-config`, 可单独查看当前验证口径、baseline 覆盖状态、待回填数和 OOS readiness。
- 已新增 `StrategyValidationStore.validation_baseline_status()`, 在实际灰度回填前可审计 current/legacy/mismatch baseline 分布、当前口径覆盖率和 primary ready 天数。
- 已新增 `StrategyValidationStore.validation_baseline_backfill_candidates()` 和 `/api/strategy-validation/backfill-current-baseline`, 支持先 dry-run 查看候选, 再显式 `execute=1` 触发 prefetch + `only_incomplete=True` 回填当前 baseline。
- `strategy_outcomes` 已持久化 `trade_cost_pct`、`primary_return_field`、`primary_return`、`primary_return_net`、`primary_holding_days`; metrics 和训练样本优先使用落库标签, 避免切换成本参数后历史净收益漂移。
- 已新增 `/api/strategy-validation/oos-report`, 基于 current baseline 输出样本天数、净收益、净胜率、CI、回撤、baseline readiness 和 validation gate 结论, 作为是否继续灰度/推广的依据。
- 验证页已接入 OOS report 状态条, 显示 `oos_passed` / `needs_backfill` / `insufficient_oos_days` / `gate_blocked`、ready 天数、净收益、净胜率、CI、回撤和覆盖率。
- 下一步仍需把 OOS report 接入定时调度/告警; 目前已具备新旧 baseline 分离、回填前审计、受控回填入口、稳定净收益标签、OOS 审计报告和前端状态展示, 但不能把未回填的新口径空样本误判为策略失效。

### 10.2 第二批: 收益模型和评分升级

| 步骤 | 当前代码入口 | 任务 | 验收标准 | 回退 |
|------|--------------|------|----------|------|
| B1 | `expected_return_model.py::predict_expected_return()` | 保持 `rank_score/expected_return_net/p_win/downside_p10` shadow 输出 | `model_confidence=low/shadow` 时不影响排序、仓位、执行 | 不展示 shadow 字段 |
| B2 | `recommendation_runtime_support.expected_return_ranking_context()` | 只有真实交易日、OOS、FDR/sign-test、CI 下界全部通过才传入 `use_expected_return_ranking=True` | `rank_score` 替代旧排序时, meta 中有门控证据 | `ENABLE_EXPECTED_RETURN_RANKING=0` |
| B3 | `probability_calibration.py`、`app_support.attach_score_calibration()` | 概率校准只做解释和仓位折扣候选, 不单独作为买入依据 | 页面清楚区分排序分、概率、预期收益 | 移除概率展示 |
| B4 | `calibrate.py::evaluate_interaction_ranker()` | 因子交互保持 shadow evaluator, 不写生产权重 | 多数 fold 改善且 FDR 通过前不接管排序 | `ENABLE_INTERACTION_TERMS=0` |
| B5 | `calibrate.py::evaluate_meta_labeling_gate()`、`meta_labeling.py` | Meta-Labeling 默认 shadow; enforce 前必须证明减少错误交易 | 使用 meta 后 OOS 净收益或回撤改善 | `META_LABELING_ENFORCE_ACTION=0` |

### 10.3 第三批: DeepSeek 节约使用和收益最大化

当前代码已经具备节约基础:
- `config.py`: `DEEPSEEK_DAILY_CALL_CAP=11`, `DEEPSEEK_DAILY_PRO_CALL_CAP=1`, `DEEPSEEK_EARLY_REVIEW_LIMIT=4`, `DEEPSEEK_LATE_FLASH_REVIEW_LIMIT=6`, `DEEPSEEK_LATE_PRO_REVIEW_LIMIT=4`。
- `config.py`: `DEEPSEEK_SCHEDULE_STRATEGIES=tomorrow_picks`, `DEEPSEEK_RERANK_DISABLED_STRATEGIES`, `ENABLE_DEEPSEEK_MARKET_GATE=0`, `DEEPSEEK_VALIDATION_REVIEW_MIN_NEW_DAYS=5` 可用于按策略收缩、避免全市场频繁调用。
- `deepseek_scheduler.py`: 交易时段窗口、候选签名、调用去抖、日内复用、usage 累计。
- `deepseek_client.py`: 单策略 rerank、batch rerank、cache hit、`cost_hint/usage` 输出。
- `recommendation_runtime_support.py`: `apply_deepseek_rerank_batch()` 已统一处理三个策略的 DeepSeek rerank。

DeepSeek 的原则: **先用本地模型缩小候选, 再用 DeepSeek 判断少数高价值边界样本; 任何 DeepSeek 调用都必须能解释它节省了损失或提升了净收益。**

| 步骤 | 当前代码入口 | 任务 | 省钱策略 | 收益最大化验收 |
|------|--------------|------|----------|----------------|
| D1 | `config.py` DeepSeek 开关 | 默认只让 `DEEPSEEK_SCHEDULE_STRATEGIES=tomorrow_picks` 进入定时 DeepSeek; `short_term` 只做本地观察, `swing_picks` 仅在事件显著时手动/灰度 | 减少无执行价值和长周期不确定调用 | DeepSeek 调用集中在最可能次日兑现的候选 |
| D2 | `recommendation_runtime_support.build_recommendation_horizons()` | 调整计划为: 本地评分、验证门控、候选压缩后再 DeepSeek; 已被验证门控归零的策略不花 DeepSeek 预算 | 避免给不可执行候选付费 | DeepSeek reviewed rows 中 `execution_allowed=false` 占比接近 0 |
| D3 | `deepseek_scheduler.scheduled_deepseek_decision()` | 沿用候选签名复用: Top3 代码、分数、风险词无实质变化时不重新调用 | 命中 `no_material_change` / `late_debounced` 时复用缓存 | cache/reuse 比例逐周提高, 不降低 OOS 表现 |
| D4 | `deepseek_client.rerank_candidates_batch()` | 优先 batch rerank, 严禁逐股循环调用; review_limit 只覆盖本地 Top-N 和边界样本 | 每次调用覆盖多个策略/候选, 降低 token 均摊成本 | `cost_hint.total_tokens / reviewed_candidate` 下降 |
| D5 | `_needs_pro_review()` | Pro 只给三类样本: Top3 分差 <=3、存在重大事件/公告/风险词、risk_penalty >=10 的高分边界候选 | 保留 `DEEPSEEK_DAILY_PRO_CALL_CAP=1`, 其余走 base | Pro 调用后的 `delta_skip_loss_avoidance` 或 `delta_avg_return_net` 为正 |
| D6 | `deepseek_client` cache | 延长稳定事件和已审候选缓存 TTL; 对相同公告/新闻生成 `evidence_hash` 复用抽取结果 | 同一事件不重复付费 | 同一 `evidence_hash` 当日不重复调用 |
| D7 | `validation_runtime_support._attach_deepseek_oos_evaluations()` | DeepSeek 规则候选最多评估前 4 条, 只把 OOS 通过规则写入权重/规则库 | 不让 LLM 频繁生成无效规则 | DeepSeek 规则通过率、失败原因可统计 |
| D8 | `strategy_validation.deepseek_attribution()`、验证看板 | 统计 DeepSeek 边际贡献: 净收益提升、回撤降低、跳过亏损、误杀盈利、token 成本 | 以收益/token 衡量调用价值 | `deepseek_value_per_1k_tokens > 0` 才扩大预算 |

### 10.4 DeepSeek 调用预算分配建议

| 场景 | 是否调用 | 模型层级 | 候选数量 | 原因 |
|------|----------|----------|----------|------|
| 盘中观察、非执行候选 | 默认不调用 | 无 | 0 | 不形成买入指令, DeepSeek 只会增加解释成本 |
| 明日优先早盘/午盘普通刷新 | 少量调用或复用 | base | Top 4 | 用于风险提示和催化剂初筛 |
| 明日优先 14:30-15:00 尾盘决策 | 调用 | base, 必要时 pro | Top 6, pro Top 4 | 最接近执行窗口, 边际价值最高 |
| Top3 分数接近且风险/事件复杂 | 调用 | pro | Top 3-4 | 本地模型难区分, DeepSeek 可能有边际信息 |
| 候选签名未变、缓存仍有效 | 不调用 | 复用 | 0 | 当前代码已有 schedule cache, 应优先复用 |
| 策略验证门控未通过 | 不调用 | 无 | 0 | 不可执行策略不应消耗 DeepSeek 预算 |
| 事件 alpha OOS 未通过 | 只抽取不加权 | base/cache | 少量 | 保留数据积累, 不影响排序 |

### 10.5 DeepSeek 利益最大化指标

DeepSeek 是否值得用, 不看"解释是否更漂亮", 只看单位成本带来的收益改善:

```text
deepseek_value =
    delta_avg_return_net * selected_count
    + avoided_loss_from_skip
    - missed_profit_from_false_skip
    - extra_turnover_cost
    - token_cost_equivalent
```

必须每日输出:
- `deepseek_call_count`, `deepseek_pro_call_count`, `deepseek_cache_hit_count`, `deepseek_total_tokens`。
- `reviewed_candidate_count`, `cost_per_reviewed_candidate`。
- `delta_avg_return_net`, `delta_sortino`, `delta_max_drawdown`。
- `skip_loss_avoidance`, `false_skip_profit_loss`。
- `deepseek_value_per_1k_tokens`。

预算调整规则:
- 若连续 20 个真实交易日 `deepseek_value_per_1k_tokens > 0` 且 OOS/FDR 通过, 才允许提高 `DEEPSEEK_DAILY_CALL_CAP` 或扩大策略范围。
- 若连续 10 个真实交易日 DeepSeek 无边际收益, 将 `DEEPSEEK_SCHEDULE_STRATEGIES` 收缩到 `tomorrow_picks` 或关闭生产影响。
- 若 Pro 调用没有显著改善边界样本, 保留 `DEEPSEEK_DAILY_PRO_CALL_CAP=1` 且只允许人工触发。
- DeepSeek 任何时候都不能绕过验证门控、成本模型、收益模型置信度和组合风险约束。

### 10.6 最小执行里程碑

| 里程碑 | 任务范围 | 产出 | 是否允许改变生产排序 |
|--------|----------|------|----------------------|
| M0: 开关盘点 | 按 10.0 固化当前默认开启、灰度、shadow、DeepSeek 节约开关 | 一张当前环境开关表和回退命令 | 否 |
| M1: 真实收益口径 | A1-A3: 主收益字段、成本、幸存者偏差、新旧 baseline 分离 | 验证库和页面主收益口径一致 | 否, 先修验证真实性 |
| M2: 保守风控上线 | A4-A6: 平滑风险、Sortino/时间衰减、组合 cap/波动率目标化 | 排序跳变减少, 组合风险约束可解释 | 只允许保守降权/降仓 |
| M3: 收益模型 shadow | B1-B3: 预期收益、概率校准、置信度字段 | `rank_score`、`expected_return_net` 与旧 `score` 的 OOS 对照 | 否 |
| M4: 排序灰度 | B2/B4/B5: OOS/FDR/CI 通过后逐策略灰度 | 明日优先先灰度, 波段后灰度, 盘中仍观察 | 是, 但必须可一键回退 |
| M5: DeepSeek 精细化 | D1-D8: 候选压缩、batch、cache、Pro边界样本、收益/token归因 | `deepseek_value_per_1k_tokens` 和误杀/避亏归因 | 只在证明边际收益后影响排序 |

执行顺序固定为: **先修验证口径和成本, 再开保守风控, 再让收益模型 shadow, 最后才让 DeepSeek 或模型接管一部分排序**。这样可以最大化收益判断的可信度, 同时把 DeepSeek 成本控制在最可能产生边际收益的候选上。

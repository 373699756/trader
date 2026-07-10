# 荐股策略评分机制改进计划

> 创建时间: 2026-07-10
> 适用模块: `stock_analyzer/scoring.py`, `stock_analyzer/calibrate.py`, `stock_analyzer/factors.py`
> 关联文档: `strategy_and_prediction.md`, `software_design.md`

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

**实现方案**: Ridge 回归替代坐标下降。

```python
from sklearn.linear_model import Ridge
from sklearn.preprocessing import PolynomialFeatures, StandardScaler
from sklearn.pipeline import make_pipeline
import numpy as np


def _build_interaction_model(samples, strategy, degree=2, alpha=1.0):
    """
    用 Ridge 回归 + 多项式特征拟合因子权重及交互项。

    Returns:
        (model, poly, scaler) 训练好的流水线组件, 以及各特征名和权重
    """
    spec = STRATEGY_COMBINERS[strategy]
    component_keys = [term["component"] for term in spec["terms"]]
    # 添加 risk_penalty 作为独立特征
    feature_keys = component_keys + ["risk_penalty"]

    X_raw = []
    y = []
    for sample in samples:
        raw = sample.get("raw") or {}
        features = [coerce_number(raw.get(key), 50.0) for key in feature_keys]
        primary_ret = coerce_number(sample.get("primary_return_net"))
        X_raw.append(features)
        y.append(primary_ret)

    X_raw = np.array(X_raw)
    y = np.array(y)

    # 标准化 + 多项式特征 + Ridge
    model = make_pipeline(
        StandardScaler(),
        PolynomialFeatures(degree=degree, include_bias=False),
        Ridge(alpha=alpha, fit_intercept=True),
    )
    model.fit(X_raw, y)

    # 提取特征名和权重
    poly = model.named_steps["polynomialfeatures"]
    feature_names = poly.get_feature_names_out(feature_keys)
    coef = model.named_steps["ridge"].coef_

    return {
        "model": model,
        "feature_names": feature_names,
        "coefficients": coef,
        "intercept": float(model.named_steps["ridge"].intercept_),
    }
```

**通过 walk-forward 验证**: Ridge 模型在每折训练集上拟合, 在测试集上预测并与 baseline 线性模型对比 OOS 改善。

**正则化强度**: `alpha` 也通过 walk-forward 交叉验证选择最优值 (`[0.1, 0.5, 1.0, 2.0, 5.0]`)。

**预期效果**:
- 捕捉因子间的非线性关系
- Ridge 正则化减少过拟合
- 只在 OOS 显著改善时启用

---

#### 2.2.2 分数→概率校准

**核心思路**: 将排序分转化为"历史上同类分数的股票次日正收益比例"。

**实现方案**: Isotonic Regression (保序回归)。

```python
from sklearn.isotonic import IsotonicRegression
import numpy as np


class ScoreCalibrator:
    """分数-概率校准器, 用历史真实结果训练。"""

    def __init__(self):
        self._calibrator = IsotonicRegression(
            y_min=0.01,  # 最低概率 1%
            y_max=0.99,  # 最高概率 99%
            out_of_bounds="clip",
            increasing=True,  # 假设高分→高概率
        )
        self._fitted = False
        self._sample_count = 0

    def fit(self, scores: list, outcomes: list):
        """
        Args:
            scores: 历史综合分列表
            outcomes: 对应次日是否正收益 (1=正, 0=负)
        """
        if len(scores) < 20:
            return
        X = np.array(sorted(scores))
        y = np.array([o for _, o in sorted(zip(scores, outcomes))])
        self._calibrator.fit(X, y)
        self._fitted = True
        self._sample_count = len(scores)

    def predict(self, score: float) -> float:
        """返回校准后的概率估计。"""
        if not self._fitted:
            return None
        return round(float(self._calibrator.predict([score])[0]), 4)

    def predict_many(self, scores: list) -> list:
        if not self._fitted:
            return [None] * len(scores)
        return [round(float(v), 4) for v in self._calibrator.predict(scores)]

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def sample_count(self) -> int:
        return self._sample_count
```

**集成方式**:

```python
# 在 scoring.py 的 score_candidates() 末尾
from .probability_calibration import load_calibrator, save_calibrator

calibrator = load_calibrator(strategy_name)
if calibrator and calibrator.is_fitted:
    for row in result_rows:
        prob = calibrator.predict(row["score"])
        if prob is not None:
            row["calibrated_probability"] = prob
            # 补充解释性标签
            if prob >= 0.65:
                row["probability_label"] = "高置信"
            elif prob >= 0.55:
                row["probability_label"] = "中等置信"
            elif prob >= 0.48:
                row["probability_label"] = "接近随机"
            else:
                row["probability_label"] = "低置信"
```

**训练触发**: 每次 `calibrate_live_weights()` 成功后, 用验证库全部样本重新训练校准器并保存到 `.runtime/score_calibrator_{strategy}.pkl`。

**前端展示**: `score_note` 从 `"综合分是量价/趋势/风险排序分，不等于上涨概率"` 升级为:
```
score_note: "综合分 72 分，历史同类信号次日正收益概率约 61%（基于最近 N 个真实样本）"
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

**校准**: 按市场状态分别训练。
- 将历史样本按 `signal_date` 当天的 `market_regime` 分组
- 每组独立进行 walk-forward 校准
- 新交易日运行时, 先判定当前 regime, 再加载对应权重

**配置开关**: `ENABLE_REGIME_SPECIFIC_WEIGHTS = True` (默认关闭, 需足够样本后才开启)

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

**模型选择**: LightGBM 二分类器 (处理非线性、特征交互、缺失值)。

```python
import lightgbm as lgb


def train_meta_model(samples: list) -> dict:
    """训练元标签模型。"""
    X = _build_meta_features(samples)
    y = _build_meta_labels(samples)

    if len(y) < 50:
        return {"fitted": False, "reason": "样本不足"}

    model = lgb.LGBMClassifier(
        objective="binary",
        metric="auc",
        num_leaves=15,         # 控制复杂度
        max_depth=4,
        learning_rate=0.03,
        n_estimators=200,
        min_child_samples=10,  # 正则化
        reg_alpha=0.1,
        reg_lambda=0.1,
        verbose=-1,
    )
    model.fit(X, y)

    return {
        "fitted": True,
        "model": model,
        "feature_names": _meta_feature_names(),
        "sample_count": len(y),
        "positive_rate": sum(y) / len(y),
    }


def meta_confidence(meta_model: dict, stock_features: dict) -> dict:
    """对单只股票输出元模型置信度。"""
    if not meta_model.get("fitted"):
        return {"confidence": None, "reason": "元模型未就绪"}

    X = _build_single_meta_features(stock_features)
    proba = meta_model["model"].predict_proba([X])[0][1]  # P(y=1)
    confidence = round(float(proba), 4)

    return {
        "confidence": confidence,
        "action": (
            "full" if confidence >= 0.65
            else "reduced" if confidence >= 0.50
            else "skip"
        ),
    }
```

**集成到现有流程**:
- `app_support.py` 的 `apply_strategy_validation_gate()` 加入元模型门控
- 当元模型置信度 < 50% 时, 将对应行降为备选观察
- 仓位公式: `position = base_position × (0.5 + 0.5 × meta_confidence)`

**训练与部署**:
- 每次 `calibrate_live_weights()` 成功后自动训练元模型
- 保存到 `.runtime/meta_model_{strategy}.pkl`
- OOS 验证: 对比是否使用元模型门控的 walk-forward metrics

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

**配置开关**: `ENABLE_ENHANCED_FACTORS = True`, 通过环境变量控制, 默认先关闭, OOS 验证通过后开启。

---

#### 2.3.3 动态仓位缩放

**现状**: 策略退场是二元的 (胜率<48% → 全部禁止)。

**改进**: 渐变降权, 连续调整仓位比例。

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

## 三、实施路线图

```
第 0 步 (前置, 必做) ── 6.4 数据与执行层修正
            │
            ├── 6.4.4 幸存者偏差修正 (最严重, 否则后续验证全失真)
            ├── 6.4.1 尾盘执行价滑点
            ├── 6.4.2 市场冲击模型
            └── 6.4.3 主题集中度联动
                    ↓ 重新基线化所有策略真实 metrics
第 1 周 ──── 2.1.1 硬阈值平滑化
            │
            ├── 2.1.2 目标函数引入 Sortino
            │
            └── 2.1.3 样本时间衰减
                    ↓ 独立 OOS 验证, 发布到生产
第 2-3 周 ── 2.2.1 Ridge + 因子交互项
            │
            ├── 2.2.2 分数→概率校准
            │
            └── 2.2.3 分市场状态独立权重
                    ↓ 独立 OOS 验证, 发布到生产
第 4-6 周 ── 6.2.1/6.2.2 波动率目标化 + 组合优化 (立竿见影)
            │
            ├── 2.3.1 Meta-Labeling 元标签模型
            │
            ├── 2.3.2 多时间框架新因子
            │
            └── 2.3.3 动态仓位缩放
                    ↓ 独立 OOS 验证, 灰度发布
后续    ──── 6.2.3/6.2.4 新 alpha 源 (事件/集成)
                    ↓ 突破纯量价框架天花板
```

每个阶段完成后:
1. 运行 `calibrate_live_weights()` 对比改进前后的 OOS metrics
2. 仅在 OOS 客观指标显著改善 (改善 > 0.05 且多数折正面) 时合入
3. 更新 `strategy_and_prediction.md` 中的策略描述
4. **第 0 步完成后必须重新基线化**: 记录修正后的真实胜率/收益, 作为后续所有改进的对比基准

---

## 四、验证与回退机制

### 4.1 验证标准

每个改进必须通过以下检验:

| 检验项 | 阈值 | 说明 |
|--------|------|------|
| OOS objective 改善 | > 0.05 | 样本外目标函数提升 |
| positive_folds | > fold_count / 2 | 多数折正面 |
| real_win_rate 不降 | ≥ baseline | 真实胜率至少不降 |
| real_avg_return 不降 | ≥ baseline | 真实平均收益至少不降 |
| 模型复杂度增量 | 可接受 | 不因增加参数导致严重过拟合 |

### 4.2 回退开关

所有新机制均通过 `config.py` 环境变量控制:

```python
# 2.1.1
USE_SMOOTH_PENALTY = os.getenv("USE_SMOOTH_PENALTY", "1") == "1"

# 2.1.2
CALIBRATE_USE_SORTINO = os.getenv("CALIBRATE_USE_SORTINO", "1") == "1"

# 2.1.3
CALIBRATE_TIME_DECAY_HALF_LIFE = int(os.getenv("CALIBRATE_TIME_DECAY_HALF_LIFE", "60"))

# 2.2.1
ENABLE_INTERACTION_TERMS = os.getenv("ENABLE_INTERACTION_TERMS", "0") == "1"

# 2.2.2
ENABLE_PROBABILITY_CALIBRATION = os.getenv("ENABLE_PROBABILITY_CALIBRATION", "0") == "1"

# 2.2.3
ENABLE_REGIME_SPECIFIC_WEIGHTS = os.getenv("ENABLE_REGIME_SPECIFIC_WEIGHTS", "0") == "1"

# 2.3.1
ENABLE_META_LABELING = os.getenv("ENABLE_META_LABELING", "0") == "1"

# 2.3.2
ENABLE_ENHANCED_FACTORS = os.getenv("ENABLE_ENHANCED_FACTORS", "0") == "1"

# 2.3.3
ENABLE_DYNAMIC_POSITION_SCALING = os.getenv("ENABLE_DYNAMIC_POSITION_SCALING", "0") == "1"

# 6.2.1 / 6.2.2
ENABLE_VOL_TARGETING = os.getenv("ENABLE_VOL_TARGETING", "0") == "1"
ENABLE_PORTFOLIO_OPTIMIZATION = os.getenv("ENABLE_PORTFOLIO_OPTIMIZATION", "0") == "1"

# 6.2.3 / 6.2.4
ENABLE_EVENT_ALPHA = os.getenv("ENABLE_EVENT_ALPHA", "0") == "1"
ENABLE_ENSEMBLE = os.getenv("ENABLE_ENSEMBLE", "0") == "1"

# 6.5 数据与执行层修正
ENABLE_SURVIVORSHIP_CORRECTION = os.getenv("ENABLE_SURVIVORSHIP_CORRECTION", "0") == "1"
ENABLE_TAIL_AUCTION_SLIPPAGE = os.getenv("ENABLE_TAIL_AUCTION_SLIPPAGE", "0") == "1"
ENABLE_MARKET_IMPACT = os.getenv("ENABLE_MARKET_IMPACT", "0") == "1"
ENABLE_REGIME_THEME_CAP = os.getenv("ENABLE_REGIME_THEME_CAP", "0") == "1"

# 6.6 统计与压力测试修正
ENABLE_STRESS_TEST = os.getenv("ENABLE_STRESS_TEST", "0") == "1"
ENABLE_CALIBRATE_FDR = os.getenv("ENABLE_CALIBRATE_FDR", "0") == "1"
```

全部设为 `"0"` 即可恢复到当前行为。**注意**: 6.5 的四个开关建议优先开启 (尤其 `ENABLE_SURVIVORSHIP_CORRECTION`), 否则后续模型优化的验证基础不可信。6.6 的 `ENABLE_CALIBRATE_FDR` 建议在启用 2.2.1/2.3.1 时同步开启。

---

## 五、风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|---------|
| 交互项/Ridge 过拟合 | 中 | L2 正则化 + walk-forward OOS 验证 |
| 概率校准过拟合 | 低 | Isotonic 保序 + 最少 20 样本才拟合 |
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
| **多重比较下假发现激增** | 高 | 2.2.1/2.3.1 启用时必须同步开启 `ENABLE_CALIBRATE_FDR` |

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
**配置**: `ENABLE_VOL_TARGETING`, `VOL_TARGET_ANNUAL = 15.0`。

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
**预期收益**: 同等胜率下回撤更小 → Sharpe 提升 → 复利优势。
**配置**: `ENABLE_PORTFOLIO_OPTIMIZATION`, `PORTFOLIO_MAX_SINGLE_WEIGHT = 0.25`。

#### 6.2.3 【高】催化剂/事件 alpha 独立成源

**现状**: 系统已有 DeepSeek 事件评分 (`sentiment.py` / `deepseek_client.py`), 但**只用于对量价候选做重排序**, 事件本身未成为独立信号源。

**核心思想**: 把"可验证催化剂"(业绩预告、政策受益、大额订单、重组、回购) 独立成一个事件驱动策略, 与量价策略并行产出候选, 再由 6.2.4 集成层合并。

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
**配置**: `ENABLE_EVENT_ALPHA`。

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
**集成点**: 在 `calibrate.py` 的 `_fit_weights()` / coordinate descent 外层加 FDR 门控; 当 2.2.1 扩展为 Ridge/多项式特征时, 该门控尤为重要。
**配置**: `ENABLE_CALIBRATE_FDR`, `CALIBRATE_FDR_Q = 0.1`。

---

### 6.7 改进实施的正确顺序（修正版）

由于 6.5 的数据/执行层修正确保了验证指标的可信度, **必须先于模型改进完成**:

```
第 0 步 (前置, 必做) ── 6.5 数据与执行层修正
    ├─ 6.5.4 幸存者偏差修正 (最严重, 否则后续所有验证都失真)
    ├─ 6.5.1 尾盘执行价滑点
    ├─ 6.5.2 市场冲击模型
    └─ 6.5.3 主题集中度联动
            ↓ 修正后重新基线化所有策略的真实 metrics
第 1 步 ── 2.1 地基改进 (平滑惩罚/Sortino/时间衰减)
第 2 步 ── 6.2.1/6.2.2 波动率目标化 + 组合优化 (立竿见影)
第 3 步 ── 2.2/2.3 模型层升级 (交互项/概率校准/Meta-Labeling)
第 4 步 ── 6.2.3/6.2.4 新 alpha 源 (事件/集成)
            │
第 5 步 ──── 6.6 统计与压力测试修正 (FDR/压力情景)
            ↓ 降低假发现率, 量化尾部风险
```

**关键判断**: 若跳过第 0 步直接做模型改进, 可能在"被高估的指标"上优化, 导致 OOS 改善是虚假的——这是比过拟合更隐蔽的陷阱。
**另一关键判断**: 6.6 的 FDR 门控应在每次校准 (2.2.1/2.3.1) 中同步启用, 否则参数空间扩大后假发现会激增。

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



那这个打分机制还有更科学更能保证收益的打分机制吗？

review上次提交看看还有没有优化的空间


## 审查结论

  不通过。当前实现不能证明、更不能保证收益，尤其不能声称“明日优先第二天正收益”。

  好的一点是：当前三个新版本都被验证门控降为 execution_allowed=false、仓位 0，今天没有直接放行交易。

  ## 主要问题

  1. P0：明日优先存在不可成交的收盘价入场假设。
     自动快照默认 15:00 才运行，stock_analyzer/config.py:96；数据库实际信号甚至保存于 16:32，却用当日信号价计算“信号价至次日收盘”收益，stock_analyzer/
     strategy_validation.py:1823。收盘后形成决策，不可能再按已经确定的收盘价成交，这会产生执行层未来函数。

  2. P0：没有足够验证证据。
      - 当前 tomorrow_picks_v8_next_day：1 条信号，0 条成熟结果。
      - 当前 swing_2_5d_v2_signal_exit：8 条信号，0 条成熟结果。
      - 盘中观察：5 条信号，0 条回填结果。
      - 唯一有结果的旧版明日优先主推样本只有 2 条、来自同一个交易日；两条都亏损，扣成本平均约 -2.18%。
      - 另有 21 条旧版明日信号一直未回填，说明结果回填链路也不完整。

  3. P1：明日优先的入场规则互相矛盾。
     主指标是 signal_next_close_return，但涨停不可买和高开超过3%的过滤条件却检查 primary_return_field == "next_close_return"，因此两个分支永远不会触
     发，stock_analyzer/strategy_validation.py:1838。必须二选一：
      - 14:50前形成信号并按真实尾盘成交价验证；或
      - 次日开盘买入，并改用开盘至收盘收益。

  4. P1：2–5日退出模拟偏乐观。
     跳空跌破止损价时仍按止损价成交；连续跌停只延迟一天便假设次日开盘可卖；移动止损还隐含日内先后顺序，stock_analyzer/risk_rules.py:94。这些都会高估可
     实现收益。

  5. P1：2–5日保存快照的兜底路径可能绕过验证门控。
     明日优先兜底会重新应用门控，但波段兜底只附加统计并直接返回旧行，stock_analyzer/app.py:884。当前快照恰好已是零仓位，但旧快照仍可能携带历史买入动
     作。

  6. P2：运行质量尚未达到生产标准。
      - 历史因子覆盖率仅约 14.81%，2–5日策略因此处于降级状态。
      - 完整测试结果：225 passed、9 failed、12 skipped。失败涉及 DeepSeek结果解析、调度限频和东方财富行情请求。

  ## 三策略判断

   策略         当前判断
  ━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   盘中观察     零仓位设计合理，但没有分钟级成交验证，不能视为荐股收益策略
  ───────────  ──────────────────────────────────────────────────────────────
   明日优先     当前版本零成熟样本，且存在收盘价执行泄漏；不能宣称次日正收益
  ───────────  ──────────────────────────────────────────────────────────────
   2–5日持有    当前版本零成熟样本，退出模拟偏乐观；不能用于真实收益判断

  建议先修正入场与成交模型、补齐回填、修复兜底门控，然后至少积累60–120个独立前瞻交易日，报告置信区间、基准超额、组合最大回撤和全部跳过样本。即使通过，
  也只能表述为“历史上具有统计优势”，不能表述为保证收益。证监会也明确将“承诺收益、保证盈利”列为高风险或违规表述：证监会风险提示
  (https://www.csrc.gov.cn/shenzhen/c105614/c7602091/content.shtml)、《证券法》第一百三十五条
  (https://www.npc.gov.cn/c2/c30834/201912/t20191231_304436.html)。


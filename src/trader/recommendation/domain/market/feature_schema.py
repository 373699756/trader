"""Pure, ordered contract for recommendation feature columns."""

from dataclasses import dataclass

from trader.recommendation.domain.market.feature_contracts import TOMORROW_RAW_ALPHA_FEATURE_MANIFEST


@dataclass(frozen=True)
class FeatureSchema:
    """Contract for one computed feature column."""

    name: str
    dtype: str  # "float" | "int" | "bool"
    missing_strategy: str = "null"  # "null" | "neutral_50" | "zero"
    description: str = ""


# Feature columns produced by FeatureBuilder._raw_features().
# All are optional float; missing is left as None and later resolved per
# the factor index in config/features.json.
FEATURE_SCHEMA_ID = "market_feature_batch"

RAW_FEATURE_SCHEMA: tuple[FeatureSchema, ...] = (
    FeatureSchema("amount_median_20d", "float", description="20日成交额中位数"),
    FeatureSchema("turnover_median_20d", "float", description="20日换手率中位数"),
    FeatureSchema("return_3d", "float", description="3日收益率"),
    FeatureSchema("return_5d", "float", description="5日收益率"),
    FeatureSchema("return_10d", "float", description="10日收益率"),
    FeatureSchema("return_20d", "float", description="20日收益率"),
    FeatureSchema("return_60d", "float", description="60日收益率"),
    FeatureSchema(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names[0], "float", description="前复权1日收益率小数"),
    FeatureSchema(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names[1], "float", description="前复权3日收益率小数"),
    FeatureSchema(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names[2], "float", description="前复权5日收益率小数"),
    FeatureSchema(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names[3], "float", description="跳过近5日的20日动量"),
    FeatureSchema(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names[4], "float", description="跳过近5日的40日动量"),
    FeatureSchema(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names[5], "float", description="跳过近5日的60日动量"),
    FeatureSchema("qfq_amihud_20d", "float", description="20日Amihud成本代理"),
    FeatureSchema("qfq_average_amount_20d", "float", description="20日平均成交额"),
    FeatureSchema("volatility_20d", "float", description="20日波动率"),
    FeatureSchema("max_drawdown_20d", "float", description="20日最大回撤"),
    FeatureSchema("atr20_pct", "float", description="20日平均真实波幅百分比"),
    FeatureSchema("ma5", "float", description="5日均线"),
    FeatureSchema("ma10", "float", description="10日均线"),
    FeatureSchema("ma20", "float", description="20日均线"),
    FeatureSchema("ma20_slope_pct", "float", description="MA20五日斜率"),
    FeatureSchema("volume_to_5d_average", "float", description="当日累计成交股数/前5个完整交易日均量"),
    FeatureSchema("prior_high_20d", "float", description="前20日最高价"),
    FeatureSchema("breakout_deviation_pct", "float", description="相对前20日高点偏离"),
    FeatureSchema("price_volume_confirmation", "float", description="量价确认"),
    FeatureSchema("moderate_daily_return", "float", description="当日涨幅适中"),
    FeatureSchema("ma20_60_position", "float", description="MA20/60位置"),
    FeatureSchema("ma20_60_structure", "float", description="MA20/60结构"),
    FeatureSchema("ma_slope", "float", description="均线斜率"),
    FeatureSchema("breakout_20d", "float", description="20日突破"),
    FeatureSchema("risk_adjusted_return_20d", "float", description="20日风险调整收益"),
    FeatureSchema("upward_consistency", "float", description="上涨一致性"),
    FeatureSchema("capacity_score", "float", description="成交容量"),
    FeatureSchema("moderate_amplitude", "float", description="振幅适中"),
    FeatureSchema("limit_distance_safety", "float", description="距涨停安全度"),
    FeatureSchema("close_location", "float", description="收盘位置"),
    FeatureSchema("price_executability", "float", description="价格可执行性"),
    FeatureSchema("ma20_deviation_inverse", "float", description="MA20偏离反向"),
    FeatureSchema("low_crowding_score", "float", description="低拥挤度"),
    FeatureSchema("limit_proximity", "float", description="涨停接近度"),
    FeatureSchema("trend_score", "float", description="趋势综合分"),
    FeatureSchema("ma20_deviation_pct", "float", description="相对MA20偏离百分点"),
)

# Feature columns that are populated by cross-section or derived signals
# (not from raw history).
DERIVED_FEATURE_SCHEMA: tuple[FeatureSchema, ...] = (
    FeatureSchema("amount_percentile_20d", "float", description="20日成交额分位"),
    FeatureSchema("speed_percentile", "float", description="涨速分位"),
    FeatureSchema("relative_strength_3d", "float", description="3日相对强度"),
    FeatureSchema("relative_strength_5d", "float", description="5日相对强度"),
    FeatureSchema("relative_strength_10d", "float", description="10日相对强度"),
    FeatureSchema("relative_strength_20d", "float", description="20日相对强度"),
    FeatureSchema("industry_strength", "float", description="行业强度"),
    FeatureSchema("industry_breadth", "float", description="行业上涨宽度"),
    FeatureSchema("industry_trend", "float", description="行业趋势"),
    FeatureSchema("market_breadth", "float", description="市场宽度"),
    FeatureSchema("low_volatility_score", "float", description="低波动分"),
    FeatureSchema("low_drawdown_score", "float", description="低回撤分"),
    FeatureSchema("news_sentiment", "float", description="新闻情绪"),
    FeatureSchema("evidence_freshness", "float", description="证据新鲜度"),
    FeatureSchema("value_score", "float", description="价值分"),
    FeatureSchema("growth_score", "float", description="成长分"),
    FeatureSchema("quality_score", "float", description="质量分"),
    FeatureSchema("industry_policy_score", "float", description="行业政策分"),
    FeatureSchema("risk_protection_score", "float", description="风险保护分"),
    FeatureSchema("latest_disclosed_report_loss", "float", description="最新正式财报亏损"),
    FeatureSchema("financial_deterioration", "float", description="财务恶化"),
    FeatureSchema("reduction_or_unlock", "float", description="减持/解禁"),
    FeatureSchema("shareholder_reduction_level", "float", description="股东减持等级"),
    FeatureSchema("unlock_risk", "float", description="已发布日程在公司策略前向窗口内的解禁等级"),
    FeatureSchema("pledge_risk", "float", description="质押风险"),
    FeatureSchema("negative_announcement_level", "float", description="负面公告等级"),
    FeatureSchema("major_shareholder_reduction", "float", description="大股东减持硬风险"),
    FeatureSchema("financial_fraud_history", "float", description="财务造假历史"),
    FeatureSchema("official_investigation_history", "float", description="正式立案调查历史"),
    FeatureSchema("major_illegal_history", "float", description="重大违法历史"),
    FeatureSchema("fund_occupation_history", "float", description="资金占用历史"),
    FeatureSchema("illegal_guarantee_history", "float", description="违规担保历史"),
    FeatureSchema("forced_delisting_risk", "float", description="强制退市程序风险"),
    FeatureSchema(
        "corporate_risk_window_unavailable",
        "float",
        description="近一年公司风险披露覆盖不完整",
    ),
    FeatureSchema("price_volume_divergence", "float", description="量价背离"),
    FeatureSchema("short_term_overheat", "float", description="短期过热极端结构"),
    FeatureSchema("intraday_reversal", "float", description="冲高回落极端结构"),
    FeatureSchema("liquidity_contraction", "float", description="流动性骤降极端结构"),
    FeatureSchema("trend_breakdown", "float", description="趋势破位极端结构"),
    FeatureSchema("tail_return_30m_pct", "float", description="尾盘30分钟原始收益"),
    FeatureSchema("tail_return_30m", "float", description="尾盘30分钟收益分"),
    FeatureSchema("tail_volume_ratio_raw", "float", description="尾盘原始量比"),
    FeatureSchema("tail_volume_ratio", "float", description="尾盘量比分"),
    FeatureSchema("entry_quality", "float", description="确定性入场质量"),
)

FEATURE_SCHEMA: tuple[FeatureSchema, ...] = (*RAW_FEATURE_SCHEMA, *DERIVED_FEATURE_SCHEMA)
FEATURE_SCHEMA_NAMES: tuple[str, ...] = tuple(item.name for item in FEATURE_SCHEMA)

if len(FEATURE_SCHEMA_NAMES) != len(set(FEATURE_SCHEMA_NAMES)):
    raise ValueError("feature schema contains duplicate feature names")


__all__ = [
    "FeatureSchema",
    "FEATURE_SCHEMA_ID",
    "RAW_FEATURE_SCHEMA",
    "DERIVED_FEATURE_SCHEMA",
    "FEATURE_SCHEMA",
    "FEATURE_SCHEMA_NAMES",
]

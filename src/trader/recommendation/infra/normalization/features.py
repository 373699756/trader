"""Convert normalized quotes and cached history into domain feature snapshots."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, TypedDict, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing_extensions import Unpack

    from trader.infra.settings.models import StrategySettings

from trader.infra.cache_contracts import BoundedCache, CacheIdentity, RequestKey
from trader.infra.market_data.history.history import (
    DailyBar,
    HistoryContext,
    HistoryProfile,
    RequireQfqHistory,
    SummarizeHistoryMetrics,
)
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.factors import Clamp, PercentileScoresWithMetadata, WeightedScore
from trader.recommendation.domain.market.feature_contracts import (
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    CalculateProfileQfqAlpha,
    QfqPriceAnchors,
)
from trader.recommendation.domain.market.models import (
    CrossSectionStats,
    FeatureSnapshot,
    MarketQuote,
)
from trader.recommendation.domain.market.news import DeriveNewsSignals, NewsSignalPolicy
from trader.recommendation.domain.market.research import (
    DeriveCorporateRiskFeatures,
    DeriveLatestDisclosedReportLoss,
    DeriveLongResearchFeatures,
    DeriveMarketRegime,
    FeatureComponentWeightPolicy,
    LongResearchInputs,
    LongResearchPolicy,
    MarketRegimePolicy,
    ResearchComponentsFromObservation,
    ResearchObservation,
)
from trader.recommendation.domain.market.tail import (
    DeriveTailSignals,
    MinuteBar,
    TailSignalEvidence,
    TailSignalPolicy,
)
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.risk.downside import DeriveEntrySetupValues
from trader.recommendation.infra.normalization.feature_math import (
    _CROSS_SECTION_FIELDS,
    _breakout_score,
    _close_location,
    _if_present,
    _industry_scores,
    _ma_deviation_inverse,
    _ma_position,
    _missing_quote_fields,
    _optional_band_score,
    _price_volume_confirmation,
    _slope_score,
    _structured_evidence,
)
from trader.recommendation.infra.normalization.feature_risks import ExtremeStructureRisks

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FeatureSchema:
    """Contract for one computed feature column."""

    name: str
    dtype: str  # "float" | "int" | "bool"
    missing_strategy: str = "null"  # "null" | "neutral_50" | "zero"
    description: str = ""


class FeatureBuildOptions(TypedDict, total=False):
    cross_section_reference: Mapping[str, Mapping[str, float | None]] | None
    cross_section_normalization_reference: Mapping[str, Mapping[str, CrossSectionStats]] | None
    research_observations: Mapping[str, ResearchObservation] | None
    intraday_minutes: Mapping[str, Sequence[MinuteBar]] | None
    history_summaries: Mapping[str, HistoryContext] | None
    preview_context: PreviewContext | None


class _FeatureGroupOptions(FeatureBuildOptions):
    data_version: str


class StandardizedFeatureBuilder(Protocol):
    def build(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        **options: Unpack[FeatureBuildOptions],
    ) -> tuple[FeatureSnapshot, ...]: ...


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
    FeatureSchema("unlock_risk", "float", description="未来90日解禁等级"),
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


@dataclass(frozen=True)
class _QuoteFactorInputs:
    """Only quote facts read by base arithmetic; observation health stays outside."""

    code: str
    observation_date: str
    price: float | None
    high: float | None
    low: float | None
    pct_change: float | None
    amount: float | None
    amplitude: float | None
    volume: float | None
    exchange_limit_pct: float | None
    has_price_limit: bool | None

    @classmethod
    def from_quote(cls, quote: MarketQuote) -> _QuoteFactorInputs:
        return cls(
            quote.code,
            quote.source_time.astimezone(_SHANGHAI).date().isoformat(),
            quote.price,
            quote.high,
            quote.low,
            quote.pct_change,
            quote.amount,
            quote.amplitude,
            quote.volume,
            quote.exchange_limit_pct,
            quote.has_price_limit,
        )


@dataclass(frozen=True)
class _CachedQuoteFactors:
    inputs: _QuoteFactorInputs
    bars: tuple[DailyBar, ...]
    context: HistoryContext | None
    values: tuple[tuple[str, float | None], ...]

    def matches(self, inputs: _QuoteFactorInputs, bars: tuple[DailyBar, ...], context: HistoryContext | None) -> bool:
        return self.inputs == inputs and self.bars == bars and self.context == context


class FeatureBuilder:
    @classmethod
    def from_settings(
        cls,
        strategy: StrategySettings,
        horizons: tuple[int, ...],
        cache: BoundedCache[object],
    ) -> FeatureBuilder:
        return cls(
            strategy.news_signal,
            strategy.tomorrow_tail_signal,
            strategy.market_regime,
            strategy.long_research,
            strategy.feature_component_weights,
            model_momentum_horizons=horizons,
            cache=cache,
        )

    def __init__(  # noqa: PLR0913 - feature builder receives explicit policy owners at the composition boundary
        self,
        news_signal_policy: NewsSignalPolicy,
        tail_signal_policy: TailSignalPolicy,
        market_regime_policy: MarketRegimePolicy,
        long_research_policy: LongResearchPolicy,
        feature_component_weights: FeatureComponentWeightPolicy,
        *,
        model_momentum_horizons: tuple[int, ...] = (20, 40, 60),
        cache: BoundedCache[object] | None = None,
    ) -> None:
        if (
            not model_momentum_horizons
            or any(horizon < 1 for horizon in model_momentum_horizons)
            or len(set(model_momentum_horizons)) != len(model_momentum_horizons)
        ):
            raise ValueError("model momentum horizons must be positive and unique")
        self._news_signal_policy = news_signal_policy
        self._tail_signal_policy = tail_signal_policy
        self._market_regime_policy = market_regime_policy
        self._long_research_policy = long_research_policy
        self._feature_component_weights = feature_component_weights
        self._model_momentum_horizons = tuple(model_momentum_horizons)
        self._cache = cache
        self._base_policy_identity = ContentHash(feature_component_weights)

    def build(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        **options: Unpack[FeatureBuildOptions],
    ) -> tuple[FeatureSnapshot, ...]:
        RequireQfqHistory(histories)
        grouped: dict[str, list[MarketQuote]] = defaultdict(list)
        for quote in quotes:
            grouped[quote.data_version].append(quote)
        built: dict[str, FeatureSnapshot] = {}
        for data_version, group_quotes in grouped.items():
            for snapshot in self._build_group(
                tuple(group_quotes),
                histories,
                observed_at,
                data_version=data_version,
                cross_section_reference=options.get("cross_section_reference"),
                cross_section_normalization_reference=options.get("cross_section_normalization_reference"),
                research_observations=options.get("research_observations"),
                intraday_minutes=options.get("intraday_minutes"),
                history_summaries=options.get("history_summaries"),
                preview_context=options.get("preview_context"),
            ):
                built[snapshot.quote.code] = snapshot
        return tuple(built[quote.code] for quote in quotes)

    def _build_group(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        **options: Unpack[_FeatureGroupOptions],
    ) -> tuple[FeatureSnapshot, ...]:
        data_version = options["data_version"]
        cross_section_reference = options.get("cross_section_reference")
        cross_section_normalization_reference = options.get("cross_section_normalization_reference")
        research_observations = options.get("research_observations")
        intraday_minutes = options.get("intraday_minutes")
        history_summaries = options.get("history_summaries")
        tail_signals = (
            {
                quote.code: DeriveTailSignals(
                    tuple(intraday_minutes.get(quote.code, ())),
                    observed_at=observed_at,
                    policy=self._tail_signal_policy,
                    preview_context=options.get("preview_context"),
                )
                for quote in quotes
            }
            if intraday_minutes is not None
            else None
        )
        raw = {
            quote.code: self._raw_features(
                quote,
                histories.get(quote.code, ()),
                (history_summaries or {}).get(quote.code),
            )
            for quote in quotes
        }
        amount_percentiles, amount_stats = PercentileScoresWithMetadata(
            {code: values.get("amount_median_20d") for code, values in raw.items()},
            population_data_version=data_version,
        )
        speed_percentiles, speed_stats = PercentileScoresWithMetadata(
            {quote.code: quote.speed for quote in quotes}, population_data_version=data_version
        )
        strength_results = {
            days: PercentileScoresWithMetadata(
                {code: values.get(f"return_{days}d") for code, values in raw.items()},
                population_data_version=data_version,
            )
            for days in (3, 5, 10, 20)
        }
        volatility_scores, volatility_stats = PercentileScoresWithMetadata(
            {code: values.get("volatility_20d") for code, values in raw.items()},
            inverse=True,
            population_data_version=data_version,
        )
        drawdown_scores, drawdown_stats = PercentileScoresWithMetadata(
            {code: values.get("max_drawdown_20d") for code, values in raw.items()},
            population_data_version=data_version,
        )
        industry_strength, industry_strength_stats, industry_breadth = _industry_scores(quotes, data_version)
        valid_changes = [
            quote.pct_change for quote in quotes if quote.pct_change is not None and math.isfinite(quote.pct_change)
        ]
        market_breadth = (
            100.0 * sum(value > 0 for value in valid_changes) / len(valid_changes) if valid_changes else None
        )
        market_breadth_stats = CrossSectionStats(
            None,
            None,
            len(valid_changes),
            len(quotes) - len(valid_changes),
            0.025,
            0.975,
            data_version,
        )
        shared_normalization = {
            "amount_percentile_20d": amount_stats,
            "speed_percentile": speed_stats,
            "relative_strength_3d": strength_results[3][1],
            "relative_strength_5d": strength_results[5][1],
            "relative_strength_10d": strength_results[10][1],
            "relative_strength_20d": strength_results[20][1],
            "industry_strength": industry_strength_stats,
            "market_breadth": market_breadth_stats,
            "low_volatility_score": volatility_stats,
            "low_drawdown_score": drawdown_stats,
        }

        snapshots: list[FeatureSnapshot] = []
        for quote in quotes:
            values = raw[quote.code]
            research_observation = (research_observations or {}).get(quote.code)
            candidate_evidence = tuple(research_observation.evidence if research_observation is not None else ())[:15]
            tail_signal = tail_signals.get(quote.code) if tail_signals is not None else None
            intraday_evidence = TailSignalEvidence(quote.code, tail_signal) if tail_signal is not None else None
            news_signals = DeriveNewsSignals(
                candidate_evidence,
                observed_at=observed_at,
                policy=self._news_signal_policy,
            )
            values.update(
                {
                    "amount_percentile_20d": _if_present(
                        values.get("amount_median_20d"), amount_percentiles[quote.code]
                    ),
                    "speed_percentile": _if_present(quote.speed, speed_percentiles[quote.code]),
                    "relative_strength_3d": _if_present(values.get("return_3d"), strength_results[3][0][quote.code]),
                    "relative_strength_5d": _if_present(values.get("return_5d"), strength_results[5][0][quote.code]),
                    "relative_strength_10d": _if_present(values.get("return_10d"), strength_results[10][0][quote.code]),
                    "relative_strength_20d": _if_present(values.get("return_20d"), strength_results[20][0][quote.code]),
                    "industry_strength": industry_strength.get(quote.industry),
                    "industry_breadth": industry_breadth.get(quote.industry),
                    "industry_trend": industry_strength.get(quote.industry),
                    "market_breadth": market_breadth,
                    "low_volatility_score": _if_present(values.get("volatility_20d"), volatility_scores[quote.code]),
                    "low_drawdown_score": _if_present(values.get("max_drawdown_20d"), drawdown_scores[quote.code]),
                    "news_sentiment": news_signals.sentiment_score,
                    "evidence_freshness": news_signals.freshness_score,
                }
            )
            values["entry_quality"] = DeriveEntrySetupValues(quote, values).score
            if tail_signal is not None:
                values.update(
                    {
                        "tail_return_30m_pct": tail_signal.return_pct,
                        "tail_return_30m": tail_signal.return_score,
                        "tail_volume_ratio_raw": tail_signal.volume_ratio,
                        "tail_volume_ratio": tail_signal.volume_score,
                    }
                )
            reference = (cross_section_reference or {}).get(quote.code, {})
            for name in _CROSS_SECTION_FIELDS:
                if name in reference:
                    values[name] = reference[name]
            market_regime = DeriveMarketRegime(values.get("market_breadth"), self._market_regime_policy)
            observation = research_observation or ResearchObservation()
            values.update(
                DeriveLongResearchFeatures(
                    observation,
                    LongResearchInputs(
                        price=quote.price,
                        industry_strength=values.get("industry_strength"),
                        low_volatility_score=values.get("low_volatility_score"),
                        low_drawdown_score=values.get("low_drawdown_score"),
                    ),
                    self._long_research_policy,
                    self._feature_component_weights,
                )
            )
            values["latest_disclosed_report_loss"] = DeriveLatestDisclosedReportLoss(observation, observed_at)
            values.update(
                DeriveCorporateRiskFeatures(
                    observation.corporate_risk_facts,
                    observed_at,
                    window_complete=observation.corporate_risk_window_complete,
                )
            )
            values.update(
                ExtremeStructureRisks(
                    quote,
                    values,
                    observed_at,
                    valid_minute_count=tail_signal.valid_bar_count if tail_signal is not None else None,
                )
            )
            normalization = dict(shared_normalization)
            normalization.update((cross_section_normalization_reference or {}).get(quote.code, {}))
            missing = tuple(
                sorted(
                    {
                        *(name for name, value in values.items() if value is None),
                        *_missing_quote_fields(quote),
                    }
                )
            )
            snapshots.append(
                FeatureSnapshot(
                    quote=quote,
                    values=values,
                    observed_at=observed_at,
                    history_days=(
                        history_summaries[quote.code].sample_count
                        if history_summaries is not None and quote.code in history_summaries
                        else len(histories.get(quote.code, ()))
                    ),
                    market_regime=market_regime,
                    missing_fields=missing,
                    evidence=(
                        _structured_evidence(quote, values, observed_at),
                        *((intraday_evidence,) if intraday_evidence is not None else ()),
                        *candidate_evidence,
                    ),
                    normalization=normalization,
                    research_components=ResearchComponentsFromObservation(research_observation),
                    preview_context=options.get("preview_context"),
                )
            )
        return tuple(snapshots)

    def _raw_features(
        self,
        quote: MarketQuote,
        bars: tuple[DailyBar, ...],
        history_summary: HistoryContext | None = None,
    ) -> dict[str, float | None]:
        inputs = _QuoteFactorInputs.from_quote(quote)
        observation_date = inputs.observation_date
        context = history_summary
        if context is not None and context.latest_trade_date > observation_date:
            context = None
            bars = tuple(bar for bar in bars if bar.trade_date < observation_date)
        base_context = (
            replace(
                context,
                sample_count=min(61, context.sample_count),
                return_anchors=tuple((lag, price) for lag, price in context.return_anchors if lag <= 60),
            )
            if context is not None
            else None
        )
        cache = self._cache
        if cache is None:
            values = self._base_features(inputs, bars, base_context)
        else:
            # Base factors depend on the same <=60-day facts in either profile.
            # Profile publication/model identities remain bound by DailyFeaturePack.
            identity = CacheIdentity(
                "quote_factor_inputs",
                "local_history",
                quote.code,
                RequestKey({"inputs": "latest"}),
                observation_date,
                "quote_factors",
                "qfq",
                self._base_policy_identity,
                FEATURE_SCHEMA_ID,
            )

            def build() -> _CachedQuoteFactors:
                found = cache.get(identity)
                if found is not None and found.value is not None:
                    cached = cast(_CachedQuoteFactors, found.value)
                    if cached.matches(inputs, bars, base_context):
                        return cached
                result = _CachedQuoteFactors(
                    inputs, bars, base_context, tuple(sorted(self._base_features(inputs, bars, base_context).items()))
                )
                cache.put(
                    identity,
                    result,
                    data_version=quote.data_version,
                    source_time=quote.source_time,
                    received_at=quote.received_time,
                )
                return result

            cached = cast(_CachedQuoteFactors, cache.coalesce(identity, build))
            # A concurrent owner may have different facts under the same latest
            # slot. Recheck the arithmetic inputs before consuming its result.
            if not cached.matches(inputs, bars, base_context):
                cached = build()
            values = dict(cached.values)
        anchor_horizons = tuple(dict.fromkeys((1, 3, 5, *self._model_momentum_horizons)))
        qfq_alpha = {
            item.feature_id.value: item.value
            for item in CalculateProfileQfqAlpha(
                QfqPriceAnchors(
                    quote.price,
                    tuple(
                        (
                            days,
                            context.anchor_price(days, observation_date)
                            if context is not None
                            else _lag_close(bars, days, observation_date),
                        )
                        for days in anchor_horizons
                    ),
                ),
                self._model_momentum_horizons,
            )
        }
        return {**values, **qfq_alpha}

    def _base_features(
        self,
        quote: _QuoteFactorInputs,
        bars: tuple[DailyBar, ...],
        history_summary: HistoryContext | None = None,
    ) -> dict[str, float | None]:
        observation_date = quote.observation_date
        context = history_summary
        if context is not None and context.latest_trade_date > observation_date:
            context = None
            bars = tuple(bar for bar in bars if bar.trade_date < observation_date)
        history = context.profile if context is not None else SummarizeHistoryMetrics(bars)
        completed_bars = tuple(bar for bar in bars if bar.trade_date < observation_date)
        if (
            context is not None
            and context.latest_trade_date >= observation_date
            and context.previous_profile is not None
        ):
            setup_history = context.previous_profile
        else:
            setup_history = SummarizeHistoryMetrics(completed_bars) if len(completed_bars) != len(bars) else history
        returns = {
            days: context.return_pct(days, quote.price, observation_date)
            if context is not None
            else _live_return_pct(bars, days, quote.price, observation_date)
            for days in (1, 3, 5, 10, 20, 40, 60)
        }
        ma5 = setup_history.moving_average_5d
        ma10 = setup_history.moving_average_10d
        ma20 = setup_history.moving_average_20d
        ma60 = history.moving_average_60d
        volatility = history.volatility_20d
        drawdown = history.max_drawdown_20d
        amount_median = history.median_amount_20d
        volume_to_5d_average = _volume_to_5d_average(quote.volume, setup_history)
        ma_position = _ma_position(quote.price, ma20, ma60)
        breakout = _breakout_score(quote.price, bars)
        slope = _slope_score(ma5, ma20)
        capacity = (
            None
            if quote.amount is None
            or not math.isfinite(quote.amount)
            or amount_median is None
            or not math.isfinite(amount_median)
            or amount_median <= 0
            else Clamp(50.0 + 25.0 * quote.amount / amount_median)
        )
        limit = quote.exchange_limit_pct or (20.0 if quote.code.startswith(("300", "301", "688", "689")) else 10.0)
        limit_proximity = (
            min(1.0, abs(quote.pct_change) / limit)
            if quote.has_price_limit is not False
            and quote.pct_change is not None
            and math.isfinite(quote.pct_change)
            and limit > 0
            else None
        )
        risk_adjusted = None
        if returns[20] is not None and volatility is not None and volatility > 0:
            risk_adjusted = Clamp(50.0 + returns[20] / volatility * 5.0)
        close_location = _close_location(quote.price, quote.high, quote.low)
        trend_score = (
            None
            if ma_position is None
            else WeightedScore(
                {"ma20_60_position": ma_position, "ma_slope": slope or 50.0},
                self._feature_component_weights.trend_score,
            )
        )
        ma20_deviation = (
            (quote.price / ma20 - 1.0) * 100.0
            if quote.price is not None and math.isfinite(quote.price) and ma20 is not None and ma20 > 0.0
            else None
        )
        prior_high = setup_history.high_20d
        breakout_deviation = (
            (quote.price / prior_high - 1.0) * 100.0
            if quote.price is not None and quote.price > 0.0 and prior_high is not None and prior_high > 0.0
            else None
        )
        return {
            "amount_median_20d": amount_median,
            "turnover_median_20d": history.median_turnover_20d,
            "return_3d": returns[3],
            "return_5d": returns[5],
            "return_10d": returns[10],
            "return_20d": returns[20],
            "return_60d": returns[60],
            "qfq_amihud_20d": history.amihud_20d,
            "qfq_average_amount_20d": history.average_amount_20d,
            "volatility_20d": volatility,
            "max_drawdown_20d": drawdown,
            "atr20_pct": setup_history.atr20_pct,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma20_slope_pct": setup_history.ma20_slope_pct,
            "volume_to_5d_average": volume_to_5d_average,
            "prior_high_20d": prior_high,
            "breakout_deviation_pct": breakout_deviation,
            "price_volume_confirmation": _price_volume_confirmation(returns[5], quote.amount, amount_median),
            "moderate_daily_return": _optional_band_score(quote.pct_change, -2.0, 0.5, 5.0, 8.0),
            "ma20_60_position": ma_position,
            "ma20_60_structure": ma_position,
            "ma_slope": slope,
            "breakout_20d": breakout,
            "risk_adjusted_return_20d": risk_adjusted,
            "upward_consistency": history.upward_consistency_20d,
            "capacity_score": capacity,
            "moderate_amplitude": _optional_band_score(quote.amplitude, 0.0, 1.0, 5.0, 12.0),
            "limit_distance_safety": None if limit_proximity is None else 100.0 * (1.0 - limit_proximity),
            "close_location": close_location,
            "price_executability": _optional_band_score(quote.price, 1.0, 5.0, 100.0, 300.0),
            "ma20_deviation_inverse": _ma_deviation_inverse(quote.price, ma20),
            "trend_score": trend_score,
            "ma20_deviation_pct": ma20_deviation,
            "low_crowding_score": None if limit_proximity is None else 100.0 * (1.0 - limit_proximity),
            "limit_proximity": limit_proximity,
            "price_volume_divergence": None,
            "latest_disclosed_report_loss": None,
            "financial_deterioration": None,
            "reduction_or_unlock": None,
            "shareholder_reduction_level": None,
            "unlock_risk": None,
            "pledge_risk": None,
            "negative_announcement_level": None,
            "major_shareholder_reduction": None,
            "financial_fraud_history": None,
            "official_investigation_history": None,
            "major_illegal_history": None,
            "fund_occupation_history": None,
            "illegal_guarantee_history": None,
            "forced_delisting_risk": None,
            "corporate_risk_window_unavailable": None,
            "news_sentiment": None,
            "evidence_freshness": None,
            "value_score": None,
            "growth_score": None,
            "quality_score": None,
            "industry_policy_score": None,
            "risk_protection_score": None,
            "short_term_overheat": None,
            "intraday_reversal": None,
            "liquidity_contraction": None,
            "trend_breakdown": None,
            "entry_quality": None,
        }


def _lag_close(bars: tuple[DailyBar, ...], days: int, observation_date: str) -> float | None:
    if days < 1:
        return None
    completed = tuple(bar for bar in bars if bar.trade_date < observation_date)
    if len(completed) < days:
        return None
    value = completed[-days].close
    return value if math.isfinite(value) and value > 0.0 else None


def _live_return_pct(
    bars: tuple[DailyBar, ...], days: int, current_price: float | None, observation_date: str
) -> float | None:
    start = _lag_close(bars, days, observation_date)
    if start is None or current_price is None or not math.isfinite(current_price) or current_price <= 0:
        return None
    return (current_price / start - 1.0) * 100.0


def _volume_to_5d_average(volume: float | None, history: HistoryProfile) -> float | None:
    average_volume = history.average_volume_5d
    if (
        volume is None
        or not math.isfinite(volume)
        or volume < 0.0
        or average_volume is None
        or not math.isfinite(average_volume)
        or average_volume <= 0.0
    ):
        return None
    return volume / average_volume


__all__ = [
    "DERIVED_FEATURE_SCHEMA",
    "FEATURE_SCHEMA",
    "FEATURE_SCHEMA_NAMES",
    "FEATURE_SCHEMA_ID",
    "FeatureBuilder",
    "FeatureSchema",
    "RAW_FEATURE_SCHEMA",
    "StandardizedFeatureBuilder",
]

"""Recommendation, feature, review and evidence snapshot codecs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime

from trader.domain.market.models import (
    Board,
    BoardPopulation,
    CrossSectionStats,
    FeatureSnapshot,
    MarketQuote,
)
from trader.domain.recommendation.downside import DownsideAssessment
from trader.domain.recommendation.models import (
    FilterAudit,
    FusionMode,
    Recommendation,
    RecommendationAction,
    ScoreBreakdown,
    Strategy,
)
from trader.infra.persistence.snapshot_primitives import (
    _integer,
    _number,
    _object,
    _optional_integer,
    _optional_number,
    _optional_text,
    _text,
)
from trader.infra.persistence.snapshot_review_items import (
    _evidence_from_dict,
    _evidence_to_dict,
    _review_from_dict,
    _review_to_dict,
    _risk_fact_from_dict,
    _risk_fact_to_dict,
)


def _recommendation_to_dict(item: Recommendation) -> dict[str, object]:
    return {
        "strategy": item.strategy.value,
        "features": _features_to_dict(item.features),
        "score": _score_to_dict(item.score),
        "local_risk_facts": [_risk_fact_to_dict(fact) for fact in item.local_risk_facts],
        "deepseek_risk_facts": [_risk_fact_to_dict(fact) for fact in item.deepseek_risk_facts],
        "review": _review_to_dict(item.review) if item.review is not None else None,
        "action": item.action.value,
        "action_reason": item.action_reason,
        "veto": item.veto,
        "rank": item.rank,
        "board_rank": item.board_rank,
        "target_price": item.target_price,
        "selection_skip_reason": item.selection_skip_reason,
        "competition_group_limit": item.competition_group_limit,
        "downside": _downside_to_dict(item.downside) if item.downside is not None else None,
    }


def _recommendation_from_dict(raw: Mapping[str, object]) -> Recommendation:
    review_raw = raw.get("review")
    local_risks = raw.get("local_risk_facts")
    deepseek_risks = raw.get("deepseek_risk_facts")
    return Recommendation(
        strategy=Strategy(_text(raw, "strategy")),
        features=_features_from_dict(_object(raw, "features")),
        score=_score_from_dict(_object(raw, "score")),
        local_risk_facts=tuple(_risk_fact_from_dict(item) for item in local_risks if isinstance(item, dict))
        if isinstance(local_risks, list)
        else (),
        deepseek_risk_facts=tuple(_risk_fact_from_dict(item) for item in deepseek_risks if isinstance(item, dict))
        if isinstance(deepseek_risks, list)
        else (),
        review=_review_from_dict(review_raw) if isinstance(review_raw, dict) else None,
        action=RecommendationAction(_text(raw, "action")),
        action_reason=_text(raw, "action_reason"),
        veto=bool(raw.get("veto")),
        rank=_integer(raw, "rank"),
        board_rank=_optional_integer(raw.get("board_rank")) or 0,
        target_price=_optional_number(raw.get("target_price")),
        selection_skip_reason=str(raw.get("selection_skip_reason") or ""),
        competition_group_limit=_optional_integer(raw.get("competition_group_limit")),
        downside=_downside_from_dict(raw.get("downside")),
    )


def _downside_to_dict(item: DownsideAssessment) -> dict[str, object]:
    return {
        "status": item.status,
        "reasons": list(item.reasons),
        "atr20_pct": item.atr20_pct,
        "intraday_reversal_atr": item.intraday_reversal_atr,
        "historical_drawdown_pct": item.historical_drawdown_pct,
        "setup_type": item.setup_type,
    }


def _downside_from_dict(raw: object) -> DownsideAssessment | None:
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "observe")
    if status not in {"pass", "observe"}:
        raise ValueError("downside status must be pass or observe")
    reasons = raw.get("reasons")
    return DownsideAssessment(
        status=status,  # type: ignore[arg-type]
        reasons=tuple(str(item) for item in reasons if isinstance(item, str)) if isinstance(reasons, list) else (),
        atr20_pct=_optional_number(raw.get("atr20_pct")),
        intraday_reversal_atr=_optional_number(raw.get("intraday_reversal_atr")),
        historical_drawdown_pct=_optional_number(raw.get("historical_drawdown_pct")),
        setup_type=str(raw.get("setup_type") or "none"),
    )


def _filter_audit_to_dict(item: FilterAudit) -> dict[str, object]:
    return {
        "stock_code": item.stock_code,
        "filter_code": item.filter_code,
        "threshold": item.threshold,
        "actual": item.actual,
        "source": item.source,
        "observed_at": item.observed_at.isoformat(),
    }


def _filter_audit_from_dict(raw: Mapping[str, object]) -> FilterAudit:
    actual = raw.get("actual")
    if actual is not None and not isinstance(actual, (str, int, float, bool)):
        raise ValueError("filter audit actual must be a JSON scalar")
    if isinstance(actual, float) and not math.isfinite(actual):
        raise ValueError("filter audit actual must be finite")
    return FilterAudit(
        stock_code=_text(raw, "stock_code"),
        filter_code=_text(raw, "filter_code"),
        threshold=_text(raw, "threshold"),
        actual=float(actual) if isinstance(actual, int) and not isinstance(actual, bool) else actual,
        source=_text(raw, "source"),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
    )


def _features_to_dict(features: FeatureSnapshot) -> dict[str, object]:
    return {
        "quote": _quote_to_dict(features.quote),
        "values": dict(features.values),
        "observed_at": features.observed_at.isoformat(),
        "history_days": features.history_days,
        "market_regime": features.market_regime,
        "missing_fields": list(features.missing_fields),
        "evidence": [_evidence_to_dict(item) for item in features.evidence],
        "external_risk_facts": [_risk_fact_to_dict(item) for item in features.external_risk_facts],
        "normalization": {
            factor_id: {
                "lower_bound": item.lower_bound,
                "upper_bound": item.upper_bound,
                "sample_size": item.sample_size,
                "missing_count": item.missing_count,
                "lower_quantile": item.lower_quantile,
                "upper_quantile": item.upper_quantile,
                "population_data_version": item.population_data_version,
            }
            for factor_id, item in features.normalization.items()
        },
        "missing_reasons": dict(features.missing_reasons),
        "board_data_reliability": features.board_data_reliability,
        "board_supported_weight": features.board_supported_weight,
        "board_policy_id": features.board_policy_id,
        "board_policy_version": features.board_policy_version,
        "board_population": _board_population_to_dict(features.board_population)
        if features.board_population is not None
        else None,
        "merge_epoch": features.merge_epoch,
        "competition_group_id": features.competition_group_id,
        "competition_group_source": features.competition_group_source,
        "competition_group_version": features.competition_group_version,
        "liquidity_bucket": features.liquidity_bucket,
        "parameter_status": features.parameter_status,
        "selection_skip_reason": features.selection_skip_reason,
    }


def _features_from_dict(raw: Mapping[str, object]) -> FeatureSnapshot:
    values = raw.get("values")
    evidence = raw.get("evidence")
    risks = raw.get("external_risk_facts")
    missing = raw.get("missing_fields")
    normalization = raw.get("normalization")
    missing_reasons = raw.get("missing_reasons")
    board_population = raw.get("board_population")
    return FeatureSnapshot(
        quote=_quote_from_dict(_object(raw, "quote")),
        values={str(key): _optional_number(value) for key, value in values.items()} if isinstance(values, dict) else {},
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        history_days=_integer(raw, "history_days"),
        market_regime=_text(raw, "market_regime"),
        missing_fields=tuple(str(value) for value in missing if isinstance(value, str))
        if isinstance(missing, list)
        else (),
        evidence=tuple(_evidence_from_dict(item) for item in evidence if isinstance(item, dict))
        if isinstance(evidence, list)
        else (),
        external_risk_facts=tuple(_risk_fact_from_dict(item) for item in risks if isinstance(item, dict))
        if isinstance(risks, list)
        else (),
        normalization={
            str(factor_id): CrossSectionStats(
                lower_bound=_optional_number(item.get("lower_bound")),
                upper_bound=_optional_number(item.get("upper_bound")),
                sample_size=_integer(item, "sample_size"),
                missing_count=_integer(item, "missing_count"),
                lower_quantile=_number(item, "lower_quantile"),
                upper_quantile=_number(item, "upper_quantile"),
                population_data_version=_text(item, "population_data_version"),
            )
            for factor_id, item in normalization.items()
            if isinstance(item, dict)
        }
        if isinstance(normalization, dict)
        else {},
        missing_reasons={str(name): str(reason) for name, reason in missing_reasons.items()}
        if isinstance(missing_reasons, dict)
        else {},
        board_data_reliability=(_optional_number(raw.get("board_data_reliability")) or 0.0)
        if raw.get("board_data_reliability") is not None
        else 1.0,
        board_supported_weight=(_optional_number(raw.get("board_supported_weight")) or 0.0)
        if raw.get("board_supported_weight") is not None
        else 1.0,
        board_policy_id=str(raw.get("board_policy_id") or ""),
        board_policy_version=str(raw.get("board_policy_version") or ""),
        board_population=_board_population_from_dict(board_population) if isinstance(board_population, dict) else None,
        merge_epoch=str(raw.get("merge_epoch") or ""),
        competition_group_id=str(raw.get("competition_group_id") or ""),
        competition_group_source=str(raw.get("competition_group_source") or ""),
        competition_group_version=str(raw.get("competition_group_version") or ""),
        liquidity_bucket=str(raw.get("liquidity_bucket") or ""),
        parameter_status=str(raw.get("parameter_status") or "current"),
        selection_skip_reason=str(raw.get("selection_skip_reason") or ""),
    )


def _board_population_to_dict(population: BoardPopulation) -> dict[str, object]:
    return {
        "trade_date": population.trade_date,
        "phase": population.phase,
        "board": population.board.value,
        "data_version": population.data_version,
        "schema_version": population.schema_version,
        "population_version": population.population_version,
        "sample_size": population.sample_size,
        "missing_count": population.missing_count,
        "liquidity_p50": population.liquidity_p50,
        "liquidity_p80": population.liquidity_p80,
        "fallback_trade_date": population.fallback_trade_date,
        "fallback_age_sessions": population.fallback_age_sessions,
        "status": population.status,
    }


def _board_population_from_dict(raw: Mapping[str, object]) -> BoardPopulation:
    return BoardPopulation(
        trade_date=_text(raw, "trade_date"),
        phase=_text(raw, "phase"),
        board=Board(_text(raw, "board")),
        data_version=_text(raw, "data_version"),
        schema_version=_text(raw, "schema_version"),
        population_version=_text(raw, "population_version"),
        sample_size=_integer(raw, "sample_size"),
        missing_count=_integer(raw, "missing_count"),
        liquidity_p50=_optional_number(raw.get("liquidity_p50")),
        liquidity_p80=_optional_number(raw.get("liquidity_p80")),
        fallback_trade_date=_optional_text(raw, "fallback_trade_date"),
        fallback_age_sessions=_optional_integer(raw.get("fallback_age_sessions")),
        status=str(raw.get("status") or "current"),  # type: ignore[arg-type]
    )


def _quote_to_dict(quote: MarketQuote) -> dict[str, object]:
    return {
        "code": quote.code,
        "name": quote.name,
        "price": quote.price,
        "previous_close": quote.previous_close,
        "open_price": quote.open_price,
        "high": quote.high,
        "low": quote.low,
        "pct_change": quote.pct_change,
        "change_5m": quote.change_5m,
        "speed": quote.speed,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "amplitude": quote.amplitude,
        "market_cap": quote.market_cap,
        "industry": quote.industry,
        "source": quote.source,
        "source_time": quote.source_time.isoformat(),
        "received_time": quote.received_time.isoformat(),
        "data_version": quote.data_version,
        "is_st": quote.is_st,
        "is_suspended": quote.is_suspended,
        "is_one_price_limit": quote.is_one_price_limit,
        "is_blacklisted": quote.is_blacklisted,
        "has_major_regulatory_risk": quote.has_major_regulatory_risk,
        "cross_source_deviation_pct": quote.cross_source_deviation_pct,
        "cross_source_verified": quote.cross_source_verified,
        "board": quote.board.value,
        "board_source": quote.board_source,
        "board_reliability": quote.board_reliability,
        "exchange": quote.exchange,
        "listing_date": quote.listing_date.isoformat() if quote.listing_date is not None else None,
        "listing_age_sessions": quote.listing_age_sessions,
        "is_relisted_first_session": quote.is_relisted_first_session,
        "is_delisting_period_first_session": quote.is_delisting_period_first_session,
        "has_price_limit": quote.has_price_limit,
        "exchange_limit_pct": quote.exchange_limit_pct,
        "strategy_hot_cap_pct": quote.strategy_hot_cap_pct,
        "rule_version": quote.rule_version,
        "rule_effective_date": quote.rule_effective_date.isoformat() if quote.rule_effective_date is not None else None,
        "execution_restrictions": list(quote.execution_restrictions),
    }


def _quote_from_dict(raw: Mapping[str, object]) -> MarketQuote:
    restrictions = raw.get("execution_restrictions")
    return MarketQuote(
        code=_text(raw, "code"),
        name=_text(raw, "name"),
        price=_optional_number(raw.get("price")),
        previous_close=_optional_number(raw.get("previous_close")),
        open_price=_optional_number(raw.get("open_price")),
        high=_optional_number(raw.get("high")),
        low=_optional_number(raw.get("low")),
        pct_change=_optional_number(raw.get("pct_change")),
        change_5m=_optional_number(raw.get("change_5m")),
        speed=_optional_number(raw.get("speed")),
        volume_ratio=_optional_number(raw.get("volume_ratio")),
        turnover_rate=_optional_number(raw.get("turnover_rate")),
        amount=_optional_number(raw.get("amount")),
        amplitude=_optional_number(raw.get("amplitude")),
        market_cap=_optional_number(raw.get("market_cap")),
        industry=str(raw.get("industry") or ""),
        source=_text(raw, "source"),
        source_time=datetime.fromisoformat(_text(raw, "source_time")),
        received_time=datetime.fromisoformat(_text(raw, "received_time")),
        data_version=_text(raw, "data_version"),
        is_st=bool(raw.get("is_st")),
        is_suspended=bool(raw.get("is_suspended")),
        is_one_price_limit=bool(raw.get("is_one_price_limit")),
        is_blacklisted=bool(raw.get("is_blacklisted")),
        has_major_regulatory_risk=bool(raw.get("has_major_regulatory_risk")),
        cross_source_deviation_pct=_optional_number(raw.get("cross_source_deviation_pct")),
        cross_source_verified=bool(raw.get("cross_source_verified", True)),
        board=Board(str(raw.get("board") or Board.UNSUPPORTED.value)),
        board_source=str(raw.get("board_source") or ""),
        board_reliability=str(raw.get("board_reliability") or "unknown"),
        exchange=str(raw.get("exchange") or ""),
        listing_date=_optional_date(raw.get("listing_date")),
        listing_age_sessions=_optional_integer(raw.get("listing_age_sessions")),
        is_relisted_first_session=_optional_boolean(raw.get("is_relisted_first_session")),
        is_delisting_period_first_session=_optional_boolean(raw.get("is_delisting_period_first_session")),
        has_price_limit=_optional_boolean(raw.get("has_price_limit")),
        exchange_limit_pct=_optional_number(raw.get("exchange_limit_pct")),
        strategy_hot_cap_pct=_optional_number(raw.get("strategy_hot_cap_pct")),
        rule_version=str(raw.get("rule_version") or ""),
        rule_effective_date=_optional_date(raw.get("rule_effective_date")),
        execution_restrictions=tuple(str(value) for value in restrictions if isinstance(value, str))
        if isinstance(restrictions, list)
        else (),
    )


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("expected an ISO date or null")
    return date.fromisoformat(value)


def _optional_boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _score_to_dict(score: ScoreBreakdown) -> dict[str, object]:
    return {
        "components": dict(score.components),
        "base_score": score.base_score,
        "local_risk_penalty": score.local_risk_penalty,
        "local_score": score.local_score,
        "deepseek_score": score.deepseek_score,
        "confidence_coverage": score.confidence_coverage,
        "deepseek_risk_penalty": score.deepseek_risk_penalty,
        "final_score": score.final_score,
        "fusion_mode": score.fusion_mode.value,
        "fusion_applied": score.fusion_applied,
    }


def _score_from_dict(raw: Mapping[str, object]) -> ScoreBreakdown:
    components = raw.get("components")
    return ScoreBreakdown(
        components={str(key): float(value) for key, value in components.items()}
        if isinstance(components, dict)
        else {},
        base_score=_number(raw, "base_score"),
        local_risk_penalty=_number(raw, "local_risk_penalty"),
        local_score=_number(raw, "local_score"),
        deepseek_score=_optional_number(raw.get("deepseek_score")),
        confidence_coverage=_number(raw, "confidence_coverage"),
        deepseek_risk_penalty=_number(raw, "deepseek_risk_penalty"),
        final_score=_number(raw, "final_score"),
        fusion_mode=FusionMode(_text(raw, "fusion_mode")),
        fusion_applied=bool(raw.get("fusion_applied")),
    )

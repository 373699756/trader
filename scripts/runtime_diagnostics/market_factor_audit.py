"""Field-level completeness projections for supplier/factor diagnostics."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime

from trader.recommendation.domain.market.factors import PRODUCTION_FACTOR_IDS
from trader.recommendation.domain.market.feature_contracts import FEATURE_SPEC_CATALOG, V2_FEATURE_SPEC_CATALOG
from trader.recommendation.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.scoring import (
    BoardCrossSectionRequest,
    BuildBoardCrossSection,
    CandidateFields,
)
from trader.recommendation.infra.normalization.merge import ObservationFromQuote

from .market_factor_inputs import FactorInputs

CORE_QUOTE_FIELDS = ("price", "pct_change", "amount")
SCORING_QUOTE_FIELDS = (
    *CORE_QUOTE_FIELDS,
    "high",
    "low",
    "turnover_rate",
    "amplitude",
)
REQUIRED_DECISION_QUOTE_FIELDS = SCORING_QUOTE_FIELDS
DERIVABLE_QUOTE_FIELDS = ("volume_ratio",)
OPTIONAL_FALLBACK_QUOTE_FIELDS = ("market_cap",)
VALIDATION_QUOTE_FIELDS = ("previous_close", "open_price")
IGNORED_REALTIME_FIELDS = ("speed", "change_5m")
QUOTE_FIELDS = (
    "price",
    "previous_close",
    "open_price",
    "high",
    "low",
    "pct_change",
    "amount",
    "turnover_rate",
    "volume_ratio",
    "market_cap",
    "amplitude",
)
_POSITIVE = frozenset({"price", "previous_close", "open_price", "high", "low", "amount", "market_cap"})
_NONNEGATIVE = frozenset({"turnover_rate", "volume_ratio", "amplitude"})
_RESEARCH = frozenset(
    {
        "value_score",
        "quality_score",
        "growth_score",
        "financial_deterioration",
        "pledge_risk",
        "unlock_risk",
        "negative_announcement_level",
        "major_shareholder_reduction",
        "financial_fraud_history",
        "official_investigation_history",
        "major_illegal_history",
        "fund_occupation_history",
        "illegal_guarantee_history",
        "forced_delisting_risk",
        "corporate_risk_window_unavailable",
        "reduction_or_unlock",
        "shareholder_reduction_level",
        "risk_protection_score",
    }
)
_MINUTES = frozenset({"tail_return_30m", "tail_return_30m_pct", "tail_volume_ratio", "tail_volume_ratio_raw"})


def ValidQuoteValue(name: str, value: object) -> bool:
    if name == "industry":
        return isinstance(value, str) and bool(value.strip())
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return False
    if name in _POSITIVE:
        return value > 0
    return name not in _NONNEGATIVE or value >= 0


def QuoteAudit(quotes: Sequence[MarketQuote], requested: tuple[str, ...], at: datetime) -> dict[str, object]:
    allowed = set(requested)
    by_code = {quote.code: quote for quote in quotes if quote.code in allowed}
    rows = {
        code: ObservationFromQuote(quote, source=quote.source, observed_at=at).fields for code, quote in by_code.items()
    }
    total = len(requested)
    fields = []
    for name in QUOTE_FIELDS:
        valid = sum(ValidQuoteValue(name, row.get(name)) for row in rows.values())
        fields.append(
            {
                "field": name,
                "required_quote_input": name in CORE_QUOTE_FIELDS,
                "scoring_input": name in SCORING_QUOTE_FIELDS,
                "decision_input": name in REQUIRED_DECISION_QUOTE_FIELDS,
                "derivable_input": name in DERIVABLE_QUOTE_FIELDS,
                "optional_fallback_input": name in OPTIONAL_FALLBACK_QUOTE_FIELDS,
                "validation_input": name in VALIDATION_QUOTE_FIELDS,
                "valid": valid,
                "missing_or_invalid": total - valid,
                "denominator": total,
                "coverage": valid / total if total else None,
            }
        )
    valid_core = sum(all(ValidQuoteValue(name, row.get(name)) for name in CORE_QUOTE_FIELDS) for row in rows.values())
    # Tencent falls back to received_at on an invalid vendor timestamp. Such
    # rows cannot establish source freshness; other adapters expose no vendor time.
    genuine_times = [
        quote for quote in by_code.values() if quote.source == "tencent" and quote.source_time != quote.received_time
    ]
    ages = [(at - quote.source_time).total_seconds() for quote in genuine_times]
    return {
        "requested": total,
        "returned": len(by_code),
        "core_complete": valid_core,
        "source_timestamp_available": len(genuine_times),
        "age_max_seconds": max(ages) if ages else None,
        "source_time_span_seconds": (
            max(q.source_time for q in genuine_times) - min(q.source_time for q in genuine_times)
        ).total_seconds()
        if genuine_times
        else None,
        "future_timestamp_rows": sum(age < 0 for age in ages),
        "fresh_source_timestamp_rows": sum(0 <= (at - q.source_time).total_seconds() <= 30 for q in genuine_times),
        "freshness_note": "Eastmoney/Sina adapters use reception time; freshness is unverified",
        "fields": fields,
    }


def MissingQuoteCodes(quotes: Sequence[MarketQuote], requested: tuple[str, ...], at: datetime) -> tuple[str, ...]:
    rows = {quote.code: ObservationFromQuote(quote, source=quote.source, observed_at=at).fields for quote in quotes}
    return tuple(
        code
        for code in requested
        if code not in rows
        or any(not ValidQuoteValue(name, rows[code].get(name)) for name in REQUIRED_DECISION_QUOTE_FIELDS)
    )


def QuoteFieldsComplete(audit: Mapping[str, object], required: tuple[str, ...]) -> bool:
    rows = audit.get("fields")
    if not isinstance(rows, list):
        return False
    missing = {
        str(row.get("field")): row.get("missing_or_invalid")
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("field"), str)
    }
    return all(missing.get(field) == 0 for field in required)


def FactorAudit(
    features: Sequence[FeatureSnapshot], inputs: FactorInputs, requested: tuple[str, ...]
) -> dict[str, object]:
    denominator = len(requested)
    allowed = set(requested)
    features = tuple(item for item in features if item.quote.code in allowed)
    # Local turnover/amount/flow factors are derived at the board boundary,
    # after FeatureBuilder. Audit that boundary rather than inventing gaps.
    features = tuple(
        item
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
        for item in BuildBoardCrossSection(
            BoardCrossSectionRequest(
                features,
                board,
                "FactorAudit",
                features[0].observed_at.date().isoformat() if features else "unknown",
                "FactorAudit",
                "FactorAudit",
            )
        ).features
    )
    consumers: dict[str, set[str]] = {}
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        for name in CandidateFields(strategy):
            consumers.setdefault(name, set()).add(strategy.value + ":candidate")
        for weights in inputs.policy.local_component_weights[strategy].values():
            for name, weight in weights.items():
                if weight > 0:
                    consumers.setdefault(name, set()).add(strategy.value + ":local_score")
        consumers.setdefault("entry_quality", set()).add(strategy.value + ":local_score")
    for name in inputs.policy.hard_filter.structured_risk_thresholds:
        consumers.setdefault(name, set()).add("structured_risk")
    for rule in inputs.policy.risk_rules.values():
        if rule.trigger_factor:
            consumers.setdefault(rule.trigger_factor, set()).add("risk")
    fields = []
    for name in sorted(PRODUCTION_FACTOR_IDS | set(consumers)):
        valid = sum(item.optional_value(name) is not None and name not in item.missing_fields for item in features)
        reasons = Counter(
            item.missing_reasons.get(name, "not_provided_or_not_derived")
            for item in features
            if item.optional_value(name) is None or name in item.missing_fields
        )
        if denominator > len(features):
            reasons["quote_or_feature_row_missing"] += denominator - len(features)
        origin = (
            "research_reference"
            if name in _RESEARCH
            else "intraday_minutes"
            if name in _MINUTES
            else "qfq_and_quote_or_cross_section"
        )
        fields.append(
            {
                "field": name,
                "consumers": sorted(consumers.get(name, ())),
                "input_family": origin,
                "valid": valid,
                "denominator": denominator,
                "missing": denominator - valid,
                "coverage": valid / denominator if denominator else None,
                "reasons": dict(reasons),
            }
        )
    catalog = V2_FEATURE_SPEC_CATALOG if inputs.profile == "v2" else FEATURE_SPEC_CATALOG
    features_by_code = {item.quote.code: item for item in features}
    models = []
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        head = next(item for item in inputs.model_heads if item.strategy is strategy)
        required_sessions = inputs.models.history_required_sessions(strategy)
        input_reasons: Counter[str] = Counter()
        for code in requested:
            history_rows = len(inputs.histories.get(code, ()))
            feature = features_by_code.get(code)
            if history_rows == 0:
                reason = "qfq_window_missing"
            elif history_rows < required_sessions:
                reason = "qfq_window_short"
            elif feature is None:
                reason = "quote_or_feature_missing"
            elif head.requires_industry and feature.model_industry is None:
                reason = "model_industry_missing"
            elif not inputs.models.is_input_eligible(strategy, feature):
                reason = "model_fields_or_industry_invalid"
            else:
                reason = "eligible"
            input_reasons[reason] += 1
        models.append(
            {
                "strategy": strategy.value,
                "history_required_sessions": required_sessions,
                "input_eligible": sum(inputs.models.is_input_eligible(strategy, item) for item in features),
                "input_reason_counts": dict(input_reasons),
                "requires_model_industry": head.requires_industry,
                "feature_contract": [
                    {
                        "field": spec.feature_id.value,
                        "unit": spec.unit,
                        "dependencies": [dep.value for dep in spec.dependencies],
                        "missing_policy": spec.missing_policy,
                        "calculator_group": spec.calculator_group,
                    }
                    for spec in (catalog.require(feature_id) for feature_id in head.feature_ids)
                ],
            }
        )
    return {
        "fields": fields,
        "required_factors_complete": all(row["missing"] == 0 for row in fields if row["consumers"])
        and all(model["input_eligible"] == denominator for model in models),
        "models": models,
        "qfq_complete_rows": sum(len(inputs.histories.get(code, ())) >= inputs.history_sessions for code in requested),
        "qfq_missing_rows": sum(not inputs.histories.get(code) for code in requested),
        "qfq_missing_codes": [code for code in requested if not inputs.histories.get(code)],
        "qfq_required_sessions": inputs.history_sessions,
        "qfq_one_session_short_rows": sum(
            len(inputs.histories.get(code, ())) == inputs.history_sessions - 1 for code in requested
        ),
        "qfq_length_distribution": dict(Counter(str(len(inputs.histories.get(code, ()))) for code in requested)),
        "qfq_short_windows": {
            code: len(inputs.histories.get(code, ()))
            for code in requested
            if 0 < len(inputs.histories.get(code, ())) < inputs.history_sessions
        },
        "model_industry_rows": sum(item.model_industry is not None for item in features),
        "research_note": "Research/minute network probes cover three board samples only; absent inputs remain missing",
        "value_coverage_note": "Derived default risk indicators are not proof of complete issuer evidence; inspect research auxiliary coverage separately",
    }


__all__ = [
    "QUOTE_FIELDS",
    "CORE_QUOTE_FIELDS",
    "SCORING_QUOTE_FIELDS",
    "REQUIRED_DECISION_QUOTE_FIELDS",
    "DERIVABLE_QUOTE_FIELDS",
    "OPTIONAL_FALLBACK_QUOTE_FIELDS",
    "VALIDATION_QUOTE_FIELDS",
    "IGNORED_REALTIME_FIELDS",
    "ValidQuoteValue",
    "QuoteAudit",
    "QuoteFieldsComplete",
    "MissingQuoteCodes",
    "FactorAudit",
]

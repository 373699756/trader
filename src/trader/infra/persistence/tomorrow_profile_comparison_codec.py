"""Explicit JSON codec for immutable Tomorrow V1/V2 research evidence."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.recommendation.models import Strategy
from trader.domain.research.paired_statistics import PreregisteredBootstrapResult
from trader.domain.research.tomorrow_profile_comparison import (
    TomorrowProfileComparisonReport,
    TomorrowProfileId,
    TomorrowProfileLayerMetrics,
    TomorrowProfilePair,
    TomorrowProfilePairManifest,
    TomorrowProfilePrediction,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def manifest_bytes(manifest: TomorrowProfilePairManifest) -> bytes:
    return _bytes(
        {
            "schema_version": manifest.schema_version,
            "spec_hash": manifest.spec_hash,
            "input_version": manifest.input_version,
            "trade_date": manifest.trade_date.isoformat(),
            "observed_at": manifest.observed_at.isoformat(),
            "active_profile_id": manifest.active_profile_id,
            "v1_model_version": manifest.v1_model_version,
            "v2_model_version": manifest.v2_model_version,
            "common_candidate_count": manifest.common_candidate_count,
            "v1_scorable_count": manifest.v1_scorable_count,
            "v2_scorable_count": manifest.v2_scorable_count,
            "pairs": [_pair_payload(item) for item in manifest.pairs],
            "deepseek_request_delta": manifest.deepseek_request_delta,
            "production_authority": manifest.production_authority,
            "content_hash": manifest.content_hash,
        }
    )


def manifest_from_bytes(payload: bytes) -> TomorrowProfilePairManifest:
    raw = _object(json.loads(payload.decode()), "Tomorrow profile manifest")
    pairs_raw = raw.get("pairs")
    if not isinstance(pairs_raw, list):
        raise TypeError("Tomorrow profile pairs must be a list")
    manifest = TomorrowProfilePairManifest(
        spec_hash=_text(raw, "spec_hash"),
        input_version=_text(raw, "input_version"),
        trade_date=date.fromisoformat(_text(raw, "trade_date")),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")).astimezone(_SHANGHAI),
        active_profile_id=_profile(raw, "active_profile_id"),
        v1_model_version=_text(raw, "v1_model_version"),
        v2_model_version=_text(raw, "v2_model_version"),
        common_candidate_count=_integer(raw, "common_candidate_count"),
        v1_scorable_count=_integer(raw, "v1_scorable_count"),
        v2_scorable_count=_integer(raw, "v2_scorable_count"),
        pairs=tuple(_pair(_object(item, "Tomorrow profile pair")) for item in pairs_raw),
        deepseek_request_delta=_integer(raw, "deepseek_request_delta"),
        production_authority=_boolean(raw, "production_authority"),
        schema_version=_text(raw, "schema_version"),
    )
    if manifest.content_hash != _text(raw, "content_hash"):
        raise ValueError("Tomorrow profile manifest content hash is invalid")
    return manifest


def outcome_bytes(outcome: RecommendationOutcome) -> bytes:
    return _bytes(
        {
            "schema_version": outcome.version,
            "snapshot_id": outcome.snapshot_id,
            "strategy": outcome.strategy.value,
            "recommend_date": outcome.recommend_date,
            "stock_code": outcome.stock_code,
            "horizon": outcome.horizon,
            "status": outcome.status,
            "settled_at": outcome.settled_at.isoformat(),
            "anchor_price": outcome.anchor_price,
            "atr20_pct": outcome.atr20_pct,
            "minimum_low": outcome.minimum_low,
            "end_close": outcome.end_close,
            "gross_return_pct": outcome.gross_return_pct,
            "benchmark_return_pct": outcome.benchmark_return_pct,
            "net_excess_return_pct": outcome.net_excess_return_pct,
            "mae_pct": outcome.mae_pct,
            "mae_atr": outcome.mae_atr,
            "severe_drawdown": outcome.severe_drawdown,
            "quality_reason": outcome.quality_reason,
        }
    )


def outcome_from_bytes(payload: bytes) -> RecommendationOutcome:
    raw = _object(json.loads(payload.decode()), "Tomorrow profile outcome")
    strategy = Strategy(_text(raw, "strategy"))
    if strategy is not Strategy.TOMORROW:
        raise ValueError("Tomorrow profile outcome strategy is invalid")
    status = _text(raw, "status")
    if status not in {"complete", "benchmark_missing", "insufficient_data"}:
        raise ValueError("Tomorrow profile outcome status is invalid")
    return RecommendationOutcome(
        snapshot_id=_text(raw, "snapshot_id"),
        strategy=strategy,
        recommend_date=_text(raw, "recommend_date"),
        stock_code=_text(raw, "stock_code"),
        horizon=_integer(raw, "horizon"),
        status=status,  # type: ignore[arg-type]
        settled_at=datetime.fromisoformat(_text(raw, "settled_at")),
        anchor_price=_number(raw, "anchor_price"),
        atr20_pct=_number(raw, "atr20_pct"),
        minimum_low=_optional_number(raw, "minimum_low"),
        end_close=_optional_number(raw, "end_close"),
        gross_return_pct=_optional_number(raw, "gross_return_pct"),
        benchmark_return_pct=_optional_number(raw, "benchmark_return_pct"),
        net_excess_return_pct=_optional_number(raw, "net_excess_return_pct"),
        mae_pct=_optional_number(raw, "mae_pct"),
        mae_atr=_optional_number(raw, "mae_atr"),
        severe_drawdown=_optional_boolean(raw, "severe_drawdown"),
        quality_reason=_string(raw, "quality_reason"),
        version=_text(raw, "schema_version"),
    )


def report_bytes(report: TomorrowProfileComparisonReport) -> bytes:
    return _bytes(report_payload(report))


def report_payload(report: TomorrowProfileComparisonReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "spec_hash": report.spec_hash,
        "independent_days": report.independent_days,
        "paired_candidates": report.paired_candidates,
        "v1": _layer_payload(report.v1),
        "v2": _layer_payload(report.v2),
        "daily_v2_minus_v1_20bp_pct": list(report.daily_v2_minus_v1_20bp_pct),
        "daily_v2_minus_v1_50bp_pct": list(report.daily_v2_minus_v1_50bp_pct),
        "daily_v2_minus_v1_100bp_pct": list(report.daily_v2_minus_v1_100bp_pct),
        "primary_bootstrap": _bootstrap_payload(report.primary_bootstrap),
        "gate_failures": list(report.gate_failures),
        "state": report.state,
        "manual_review_eligible": report.manual_review_eligible,
        "production_authority": report.production_authority,
        "automatic_profile_switch": report.automatic_profile_switch,
        "content_hash": report.content_hash,
    }


def report_identity_from_bytes(payload: bytes) -> tuple[str, str]:
    raw = _object(json.loads(payload.decode()), "Tomorrow profile report")
    return _text(raw, "spec_hash"), _text(raw, "content_hash")


def _pair_payload(pair: TomorrowProfilePair) -> dict[str, object]:
    return {
        "schema_version": pair.schema_version,
        "input_version": pair.input_version,
        "trade_date": pair.trade_date.isoformat(),
        "code": pair.code,
        "board": pair.board,
        "industry": pair.industry,
        "anchor_price": pair.anchor_price,
        "atr20_pct": pair.atr20_pct,
        "v1": _prediction_payload(pair.v1),
        "v2": _prediction_payload(pair.v2),
        "content_hash": pair.content_hash,
    }


def _prediction_payload(value: TomorrowProfilePrediction) -> dict[str, object]:
    return {
        "profile_id": value.profile_id,
        "model_version": value.model_version,
        "predicted_excess_return_pct": value.predicted_excess_return_pct,
        "estimated_cost_pct": value.estimated_cost_pct,
        "predicted_net_excess_pct": value.predicted_net_excess_pct,
        "signal_score": value.signal_score,
        "local_score": value.local_score,
        "model_disagreement_pct": value.model_disagreement_pct,
        "action": value.action,
        "selected": value.selected,
        "rank": value.rank,
    }


def _pair(raw: dict[str, object]) -> TomorrowProfilePair:
    pair = TomorrowProfilePair(
        input_version=_text(raw, "input_version"),
        trade_date=date.fromisoformat(_text(raw, "trade_date")),
        code=_text(raw, "code"),
        board=_text(raw, "board"),
        industry=_text(raw, "industry"),
        anchor_price=_number(raw, "anchor_price"),
        atr20_pct=_optional_number(raw, "atr20_pct"),
        v1=_prediction(_object(raw.get("v1"), "Tomorrow V1 prediction")),
        v2=_prediction(_object(raw.get("v2"), "Tomorrow V2 prediction")),
        schema_version=_text(raw, "schema_version"),
    )
    if pair.content_hash != _text(raw, "content_hash"):
        raise ValueError("Tomorrow profile pair content hash is invalid")
    return pair


def _prediction(raw: dict[str, object]) -> TomorrowProfilePrediction:
    return TomorrowProfilePrediction(
        profile_id=_profile(raw, "profile_id"),
        model_version=_text(raw, "model_version"),
        predicted_excess_return_pct=_number(raw, "predicted_excess_return_pct"),
        estimated_cost_pct=_number(raw, "estimated_cost_pct"),
        predicted_net_excess_pct=_number(raw, "predicted_net_excess_pct"),
        signal_score=_number(raw, "signal_score"),
        local_score=_number(raw, "local_score"),
        model_disagreement_pct=_number(raw, "model_disagreement_pct"),
        action=_text(raw, "action"),
        selected=_boolean(raw, "selected"),
        rank=_integer(raw, "rank"),
    )


def _layer_payload(value: TomorrowProfileLayerMetrics) -> dict[str, object]:
    return {
        "profile_id": value.profile_id,
        "candidate_pairs": value.candidate_pairs,
        "portfolio_days": value.portfolio_days,
        "mean_candidate_net_excess_pct": value.mean_candidate_net_excess_pct,
        "mean_rank_ic": value.mean_rank_ic,
        "top_bottom_quintile_spread_pct": value.top_bottom_quintile_spread_pct,
        "mean_portfolio_net_excess_20bp_pct": value.mean_portfolio_net_excess_20bp_pct,
        "mean_portfolio_net_excess_50bp_pct": value.mean_portfolio_net_excess_50bp_pct,
        "mean_portfolio_net_excess_100bp_pct": value.mean_portfolio_net_excess_100bp_pct,
        "severe_loss_rate": value.severe_loss_rate,
        "mean_turnover": value.mean_turnover,
        "maximum_stock_positive_fraction": value.maximum_stock_positive_fraction,
        "top_five_positive_fraction": value.top_five_positive_fraction,
    }


def _bootstrap_payload(value: PreregisteredBootstrapResult) -> dict[str, object]:
    return {
        "block_days": value.block_days,
        "seed": value.seed,
        "repetitions": value.repetitions,
        "sample_count": value.sample_count,
        "observed_mean": value.observed_mean,
        "confidence_lower": value.confidence_lower,
        "confidence_upper": value.confidence_upper,
        "p_value": value.p_value,
        "extreme_count": value.extreme_count,
        "paired_metric_observed_mean": value.paired_metric_observed_mean,
        "paired_metric_confidence_lower": value.paired_metric_confidence_lower,
        "paired_metric_confidence_upper": value.paired_metric_confidence_upper,
        "valid": value.valid,
        "invalid_reason": value.invalid_reason,
    }


def _bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object")
    return value


def _text(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Tomorrow profile {name} must be non-empty text")
    return value


def _string(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise TypeError(f"Tomorrow profile {name} must be text")
    return value


def _profile(raw: dict[str, object], name: str) -> TomorrowProfileId:
    value = _text(raw, name)
    if value not in {"v1", "v2"}:
        raise ValueError("Tomorrow profile id is invalid")
    return value  # type: ignore[return-value]


def _integer(raw: dict[str, object], name: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Tomorrow profile {name} must be an integer")
    return value


def _number(raw: dict[str, object], name: str) -> float:
    value = raw.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"Tomorrow profile {name} must be numeric")
    return float(value)


def _optional_number(raw: dict[str, object], name: str) -> float | None:
    return None if raw.get(name) is None else _number(raw, name)


def _boolean(raw: dict[str, object], name: str) -> bool:
    value = raw.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"Tomorrow profile {name} must be boolean")
    return value


def _optional_boolean(raw: dict[str, object], name: str) -> bool | None:
    return None if raw.get(name) is None else _boolean(raw, name)


__all__ = [
    "manifest_bytes",
    "manifest_from_bytes",
    "outcome_bytes",
    "outcome_from_bytes",
    "report_bytes",
    "report_identity_from_bytes",
    "report_payload",
]

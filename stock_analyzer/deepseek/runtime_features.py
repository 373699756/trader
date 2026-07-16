from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List

from .. import config
from ..normalization import normalize_code
from ..strategies.types import storage_strategy_name
from .feature_schema import FEATURE_SCHEMA_VERSION, adapt_feature_to_strategy, prompt_version
from .meta_model import load_meta_artifact, score_meta_features


_FEATURE_FIELDS = (
    "event_type",
    "event_direction",
    "event_strength",
    "event_reliability",
    "novelty",
    "priced_in",
    "overnight_risk",
    "regulatory_risk",
    "theme_truth",
    "uncertainty",
    "strategy_fit",
    "horizon_fit",
    "deepseek_score",
    "confidence",
    "veto",
    "risk_penalty",
    "horizon_match",
    "abstain",
    "value_quality",
    "financial_health",
    "market_flow",
    "industry_policy",
    "risk_assessment",
    "horizon_support",
)
_DERIVED_FIELDS = (
    "deepseek_features",
    "deepseek_score",
    "deepseek_selected",
    "deepseek_veto",
    "deepseek_strategy_fit",
    "deepseek_horizon_fit",
    "deepseek_period_mismatch",
    "deepseek_reason",
    "deepseek_risk_flags",
    "deepseek_risk_penalty",
    "deepseek_production_applied",
    "deepseek_meta_shadow",
    "deepseek_meta_production_applied",
)


def hard_feature_cutoff(signal_time: str = "") -> str:
    value = str(signal_time or datetime.now().isoformat(timespec="seconds"))
    day = value[:10] if len(value) >= 10 else datetime.now().date().isoformat()
    cutoff = str(getattr(config, "DEEPSEEK_PRECOMPUTE_DEADLINE", "14:48") or "14:48")[:5]
    hard_cutoff = "{}T{}:00".format(day, cutoff)
    normalized_value = value.replace(" ", "T", 1)
    if len(normalized_value) >= 16 and normalized_value[:10] == day:
        return min(normalized_value[:19], hard_cutoff)
    return hard_cutoff


def attach_persisted_deepseek_features(
    rows: Iterable[Dict[str, object]],
    strategy_name: str,
    validation_store,
    *,
    signal_time: str = "",
    cutoff_at: str = "",
) -> List[Dict[str, object]]:
    """Read persisted features only; this function deliberately has no API dependency."""
    strategy = storage_strategy_name(strategy_name)
    result = [dict(row) for row in rows or [] if isinstance(row, dict)]
    cutoff = str(cutoff_at or hard_feature_cutoff(signal_time))
    for row in result:
        _clear_attached_feature(row)
    if not bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True)):
        for row in result:
            row["deepseek_feature_status"] = "local_only"
            row["deepseek_feature_cutoff"] = cutoff
        return result
    feature_map: Dict[str, Dict[str, object]] = {}
    if validation_store is not None and result:
        try:
            feature_map = validation_store.latest_deepseek_candidate_features(
                strategy,
                [row.get("code") for row in result],
                cutoff,
                prompt_version=prompt_version(strategy),
                model_name=str(getattr(config, "DEEPSEEK_FEATURE_MODEL", "deepseek-v4-flash")),
                feature_schema_version=FEATURE_SCHEMA_VERSION,
            )
        except Exception:
            feature_map = {}
    artifact = load_meta_artifact(strategy)
    for row in result:
        feature = feature_map.get(normalize_code(row.get("code")))
        if not feature:
            row["deepseek_feature_status"] = "local_only"
            row["deepseek_feature_cutoff"] = cutoff
            continue
        feature = adapt_feature_to_strategy(feature, strategy)
        row["deepseek_features"] = feature
        row["deepseek_feature_status"] = (
            "abstain"
            if feature.get("abstain")
            else "cache_hit"
            if feature.get("batch_status") == "cache_hit"
            else "precomputed"
        )
        row["deepseek_feature_cutoff"] = cutoff
        for key in _FEATURE_FIELDS:
            row["deepseek_{}".format(key)] = feature.get(key)
        meta = score_meta_features(row, feature, artifact)
        if meta.get("available"):
            row["deepseek_meta_shadow"] = meta
            row["deepseek_meta_production_applied"] = bool(
                getattr(config, "DEEPSEEK_META_PRODUCTION_ENABLED", False) and meta.get("production_eligible")
            )
        else:
            row["deepseek_meta_production_applied"] = False
    return result


def _clear_attached_feature(row: Dict[str, object]) -> None:
    for key in _DERIVED_FIELDS:
        row.pop(key, None)
    for key in _FEATURE_FIELDS:
        row.pop("deepseek_{}".format(key), None)

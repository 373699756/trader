import math
from typing import Dict, List, Tuple

import pandas as pd

from . import config
from .app_runtime_support import (
    finalize_deepseek_meta,
    risk_blacklist_summary,
    skipped_deepseek_meta,
)
from .app_support import (
    apply_strategy_validation_gate,
    apply_tomorrow_validation_gate,
    attach_validation_summary,
    demote_strategy_rows_to_backup,
    demote_tomorrow_rows_to_backup,
    validation_gate_window_days,
)
from .calibrate import evaluate_expected_return_ranker
from .deepseek.budget import latest_strategy_batch, usage_summary
from .deepseek.production_merge import (
    attach_and_merge_rows,
    deepseek_meta_for_rows,
    merge_and_rank_rows,
    ranking_groups,
    today_phase,
)
from .expected_return_model import (
    build_expected_return_artifact,
    expected_return_artifact_promotion_gate,
    load_expected_return_artifact,
    save_expected_return_artifact,
)
from .long_term_watch import LongTermWatchScorer
from .normalization import coerce_number
from .production_baseline import attach_generation_provenance
from .scoring_core.theme_limits import limit_theme_concentration
from .validation_policy import validation_baseline_config
from .strategies import score_swing_2_5d_picks, score_today_picks, score_tomorrow_picks, storage_strategy_name


_DEEPSEEK_PUBLIC_STATUSES = {
    "precomputed",
    "cache_hit",
    "local_only",
    "abstain",
    "daily_call_limit",
    "deadline_skipped",
    "disabled",
    "error",
}
_DEEPSEEK_OPERATIONAL_STATUS_ALIASES = {
    "deadline": "deadline_skipped",
    "disabled": "disabled",
    "daily_call_limit": "daily_call_limit",
}
_DEEPSEEK_STATUS_PRIORITY = (
    "error",
    "daily_call_limit",
    "deadline_skipped",
    "disabled",
    "abstain",
    "cache_hit",
    "precomputed",
    "local_only",
)


def _deepseek_item_status(item: Dict[str, object]) -> str:
    status = str(item.get("status") or "").strip()
    if status in _DEEPSEEK_PUBLIC_STATUSES:
        return status
    error_type = str(item.get("error_type") or "").strip()
    if error_type in _DEEPSEEK_OPERATIONAL_STATUS_ALIASES:
        return _DEEPSEEK_OPERATIONAL_STATUS_ALIASES[error_type]
    if status == "no_evidence":
        return "abstain"
    if status in {"partial", "late_shadow"}:
        return "error" if item.get("error_type") or item.get("error_message") else "local_only"
    if error_type or item.get("error_message"):
        return "error"
    return "local_only"


def _overall_deepseek_status(
    strategy_rows: List[Dict[str, object]],
    *,
    production_applied: bool,
) -> str:
    """Collapse per-strategy DeepSeek states without hiding operational outcomes."""
    if production_applied:
        applied_rows = [item for item in strategy_rows if bool(item.get("production_applied"))]
        if applied_rows and all(_deepseek_item_status(item) == "cache_hit" for item in applied_rows):
            return "cache_hit"
        return "precomputed"

    statuses = [_deepseek_item_status(item) for item in strategy_rows]
    for status in _DEEPSEEK_STATUS_PRIORITY:
        if status in statuses:
            return status
    return "local_only"


def scored_strategy_rows(
    strategy_name: str,
    candidates: pd.DataFrame,
    top_n: int,
    market: str,
    market_regime: Dict[str, object],
    apply_deepseek: bool = True,
    validation_store=None,
) -> Tuple[List[Dict[str, object]], Dict[str, object], Dict[str, object]]:
    expected_context = expected_return_ranking_context(
        strategy_name,
        validation_store=validation_store,
        top_k=max(1, min(10, int(top_n or 10))),
    )
    expected_kwargs = {
        "expected_return_samples": expected_context["samples"],
        "use_expected_return_ranking": expected_context["use_ranking"],
    }
    if apply_deepseek:
        expected_kwargs["scoring_context"] = _deepseek_scoring_context(strategy_name, validation_store)
    if strategy_name == "tomorrow_picks":
        rows, meta = score_tomorrow_picks(
            candidates,
            top_n=top_n,
            market_filter=market,
            market_regime=market_regime,
            **expected_kwargs,
        )
    elif strategy_name == "swing_picks":
        rows, meta = score_swing_2_5d_picks(
            candidates,
            top_n=top_n,
            market_filter=market,
            market_regime=market_regime,
            **expected_kwargs,
        )
    else:
        raise ValueError(f"Unsupported strategy for scored rows: {strategy_name}")
    meta["expected_return_ranking"] = expected_context["meta"]
    if apply_deepseek:
        rows = merge_and_rank_rows(rows, strategy_name)
        deepseek_meta = _persisted_feature_meta(strategy_name, rows, validation_store)
    else:
        deepseek_meta = skipped_deepseek_meta(strategy_name)
    finalize_deepseek_meta(meta, rows, deepseek_meta)
    attach_generation_provenance(meta, strategy_name, rows, candidates)
    return rows, meta, deepseek_meta


def apply_deepseek_to_reviewable_rows(
    strategy_name: str,
    rows: List[Dict[str, object]],
    market: str,
    meta: Dict[str, object] = None,
    validation_store=None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    source_rows = list(rows or [])
    if not source_rows:
        deepseek_meta = {"enabled": False, "status": "local_only", "strategy": strategy_name}
        result_rows = source_rows
    else:
        result_rows = attach_and_merge_rows(source_rows, strategy_name, validation_store)
        deepseek_meta = _persisted_feature_meta(strategy_name, result_rows, validation_store)
    if meta is not None:
        finalize_deepseek_meta(meta, result_rows, deepseek_meta)
    return result_rows, deepseek_meta


def _deepseek_scoring_context(strategy_name: str, validation_store) -> Dict[str, object]:
    return {
        "_post_score_rows": lambda rows: attach_and_merge_rows(
            rows,
            strategy_name,
            validation_store,
        )
    }


def _persisted_feature_meta(
    strategy_name: str,
    rows: List[Dict[str, object]],
    validation_store=None,
) -> Dict[str, object]:
    meta = deepseek_meta_for_rows(rows, strategy_name)
    meta.update(usage_summary(validation_store))
    batch = latest_strategy_batch(validation_store, strategy_name)
    if batch:
        batch_status = str(batch.get("status") or "")
        meta.update({key: value for key, value in batch.items() if key != "status"})
        meta["coverage_pct"] = round(meta.get("reviewed", 0) * 100.0 / max(1, meta.get("requested", 0)), 2)
        if meta.get("production_applied"):
            meta["status"] = "cache_hit" if batch_status == "cache_hit" else "precomputed"
        else:
            meta["status"] = {
                "no_evidence": "abstain",
                "daily_call_limit": "daily_call_limit",
                "deadline_skipped": "deadline_skipped",
                "disabled": "disabled",
                "error": "error",
                "partial": "error",
            }.get(batch_status, meta.get("status", "local_only"))
    meta["source"] = "validation_db_point_in_time_cache"
    meta["ranking_groups"] = ranking_groups(rows, top_k=5)
    meta["reason"] = (
        "使用截止时间前完成的五维结构化审查，以25%权重参与综合评分；API失败或缺失时回退本地分。"
        if meta.get("production_applied")
        else "未取得可用DeepSeek审查，保持本地策略结果。"
    )
    if storage_strategy_name(strategy_name) == "today_term":
        meta["today_phase"] = today_phase()
    return meta


def expected_return_ranking_context(
    strategy_name: str,
    validation_store=None,
    *,
    top_k: int = 10,
    days: int = None,
) -> Dict[str, object]:
    enabled = bool(getattr(config, "ENABLE_EXPECTED_RETURN_RANKING", False))
    min_real_days = int(getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60))
    validation_baseline = validation_baseline_config(strategy_name)
    baseline_id = str(validation_baseline.get("baseline_id") or "")
    meta = {
        "enabled": enabled,
        "strategy": strategy_name,
        "status": "disabled" if not enabled else "pending",
        "min_real_days": min_real_days,
        "validation_baseline_id": baseline_id,
    }
    if strategy_name not in {"tomorrow_picks", "swing_picks"}:
        meta["status"] = "unsupported_strategy"
        return {"samples": [], "use_ranking": False, "meta": meta}
    if not enabled:
        return {"samples": [], "use_ranking": False, "meta": meta}
    if validation_store is None:
        meta["status"] = "missing_validation_store"
        return {"samples": [], "use_ranking": False, "meta": meta}
    sample_days = int(days or max(180, min_real_days))
    try:
        samples = validation_store.live_weight_samples(strategy_name, days=sample_days)
    except Exception as exc:
        meta["status"] = "sample_load_failed"
        meta["error"] = str(exc)
        return {"samples": [], "use_ranking": False, "meta": meta}
    samples = [sample for sample in samples or [] if isinstance(sample, dict)]
    day_count = len({str(sample.get("signal_date") or "") for sample in samples if sample.get("signal_date")})
    meta["sample_count"] = len(samples)
    meta["real_day_count"] = day_count
    meta["sample_days"] = sample_days
    if day_count < min_real_days:
        meta["status"] = "insufficient_real_days"
        return {"samples": samples, "use_ranking": False, "meta": meta}
    artifact_load = load_expected_return_artifact(strategy_name, baseline_id=baseline_id)
    artifact = artifact_load.get("artifact") if artifact_load.get("ok") else None
    if artifact is not None:
        meta["artifact"] = _expected_return_artifact_meta(
            artifact, artifact_load.get("status"), artifact_load.get("path")
        )
        gate_meta = _expected_return_gate_meta(artifact.get("oos_result") if isinstance(artifact, dict) else {})
        meta["gate"] = gate_meta
    else:
        meta["artifact"] = {
            "status": artifact_load.get("status", "missing"),
            "path": artifact_load.get("path", ""),
        }
        try:
            gate = evaluate_expected_return_ranker(strategy_name, samples, top_k=max(1, int(top_k or 10)))
        except Exception as exc:
            meta["status"] = "gate_failed"
            meta["error"] = str(exc)
            return {"samples": samples, "use_ranking": False, "meta": meta}
        gate_meta = _expected_return_gate_meta(gate)
        meta["gate"] = gate_meta
        artifact = build_expected_return_artifact(
            strategy_name,
            samples,
            baseline_id=baseline_id,
            oos_result=gate,
            top_k=max(1, int(top_k or 10)),
            training_days=sample_days,
        )
        try:
            path = save_expected_return_artifact(artifact)
            meta["artifact"] = _expected_return_artifact_meta(artifact, "written", path)
        except Exception as exc:
            meta["status"] = "artifact_write_failed"
            meta["artifact"] = {
                **_expected_return_artifact_meta(artifact, "write_failed", ""),
                "error": str(exc),
            }
            return {"samples": samples, "use_ranking": False, "meta": meta}
    promotion = expected_return_artifact_promotion_gate(artifact, baseline_id=baseline_id)
    meta["promotion_gate"] = promotion
    if promotion.get("can_promote"):
        meta["status"] = "active"
        return {"samples": samples, "use_ranking": True, "meta": meta}
    meta["status"] = str(promotion.get("status") or gate_meta.get("status") or "shadow_only")
    return {"samples": samples, "use_ranking": False, "meta": meta}


def _expected_return_gate_meta(gate: Dict[str, object]) -> Dict[str, object]:
    gate_meta = dict(gate or {})
    gate_meta.pop("folds", None)
    return gate_meta


def _expected_return_artifact_meta(artifact: Dict[str, object], status: object, path: object) -> Dict[str, object]:
    artifact = artifact if isinstance(artifact, dict) else {}
    return {
        "status": str(status or ""),
        "path": str(path or ""),
        "model_confidence": artifact.get("model_confidence"),
        "baseline_id": artifact.get("baseline_id"),
        "sample_count": artifact.get("sample_count"),
        "training_window": artifact.get("training_window") or {},
        "created_at": artifact.get("created_at"),
        "expires_at": artifact.get("expires_at"),
    }


def prediction_strategy_rows(
    candidates: pd.DataFrame,
    top_n: int,
    market_regime: Dict[str, object],
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    short_term_rows_override: List[Dict[str, object]] = None,
    short_term_meta_override: Dict[str, object] = None,
    cached_metrics_fn=None,
    validation_store=None,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, object]]]:
    today_rows, today_meta = score_today_picks(
        candidates,
        hot_ranks=hot_ranks,
        industry_strength=industry_strength,
        sentiment_lookup=sentiment_lookup,
        top_n=top_n,
        market_regime=market_regime,
    )
    short_rows = today_rows.get("today_term", [])
    if short_term_rows_override is not None:
        short_rows = list(short_term_rows_override)
        short_deepseek_meta = skipped_deepseek_meta(
            "today_term",
            status="snapshot_override",
            reason="Short-term rows came from the recommendation snapshot override.",
        )
    else:
        short_rows, short_deepseek_meta = apply_deepseek_to_reviewable_rows(
            "today_term",
            short_rows,
            "all",
            validation_store=validation_store,
        )

    tomorrow_rows, tomorrow_meta, _ = scored_strategy_rows(
        "tomorrow_picks",
        candidates,
        top_n=top_n,
        market="all",
        market_regime=market_regime,
        apply_deepseek=False,
        validation_store=validation_store,
    )
    if callable(cached_metrics_fn):
        try:
            apply_tomorrow_validation_gate(
                tomorrow_rows,
                tomorrow_meta,
                cached_metrics_fn("tomorrow_picks", validation_gate_window_days()),
            )
        except Exception as exc:
            reason = "验证指标读取失败，暂停重点观察并仅保留备选：{}".format(exc)
            tomorrow_meta["validation_gate"] = {
                "state": "unavailable",
                "blocked": True,
                "allows_backup": True,
                "reason": reason,
            }
            demote_tomorrow_rows_to_backup(tomorrow_rows, tomorrow_meta, reason)
    tomorrow_rows, _ = apply_deepseek_to_reviewable_rows(
        "tomorrow_picks",
        tomorrow_rows,
        "all",
        tomorrow_meta,
        validation_store=validation_store,
    )
    swing_rows, swing_meta, _ = scored_strategy_rows(
        "swing_picks",
        candidates,
        top_n=top_n,
        market="all",
        market_regime=market_regime,
        apply_deepseek=False,
        validation_store=validation_store,
    )
    if callable(cached_metrics_fn):
        _apply_validation_gate_safe("swing_picks", swing_rows, swing_meta, cached_metrics_fn)
    swing_rows, _ = apply_deepseek_to_reviewable_rows(
        "swing_picks",
        swing_rows,
        "all",
        swing_meta,
        validation_store=validation_store,
    )
    rows_by_strategy = {
        "today_term": short_rows,
        "tomorrow_picks": tomorrow_rows,
        "swing_picks": swing_rows,
    }
    metas_by_strategy = {
        "today_term": {**today_meta, "deepseek": short_deepseek_meta, **(short_term_meta_override or {})},
        "tomorrow_picks": tomorrow_meta,
        "swing_picks": swing_meta,
    }
    for strategy_name, strategy_rows in rows_by_strategy.items():
        attach_generation_provenance(metas_by_strategy[strategy_name], strategy_name, strategy_rows, candidates)
    return rows_by_strategy, metas_by_strategy


def build_recommendation_horizons(
    candidates: pd.DataFrame,
    top_n: int,
    market: str,
    market_regime: Dict[str, object],
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    cached_metrics_fn,
    apply_deepseek: bool = True,
    validation_store=None,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object], Dict[str, object]]:
    service = RecommendationService(
        cached_metrics_fn=cached_metrics_fn,
        validation_store=validation_store,
    )
    return service.build_horizons(
        candidates=candidates,
        top_n=top_n,
        market=market,
        market_regime=market_regime,
        hot_ranks=hot_ranks,
        industry_strength=industry_strength,
        sentiment_lookup=sentiment_lookup,
        apply_deepseek=apply_deepseek,
    )


class RecommendationService:
    """Application service for scoring, gating, optional DeepSeek review, and horizon meta."""

    def __init__(self, cached_metrics_fn, validation_store=None) -> None:
        self.cached_metrics_fn = cached_metrics_fn
        self.validation_store = validation_store

    def build_horizons(
        self,
        *,
        candidates: pd.DataFrame,
        top_n: int,
        market: str,
        market_regime: Dict[str, object],
        hot_ranks: Dict[str, int],
        industry_strength: Dict[str, float],
        sentiment_lookup: Dict[str, Dict[str, object]],
        apply_deepseek: bool = True,
    ) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object], Dict[str, object]]:
        recommendations_by_horizon, short_meta = score_today_picks(
            candidates,
            hot_ranks=hot_ranks,
            industry_strength=industry_strength,
            sentiment_lookup=sentiment_lookup,
            top_n=top_n,
            market_filter=market,
            market_regime=market_regime,
            scoring_context=(
                _deepseek_scoring_context("today_term", self.validation_store) if apply_deepseek else None
            ),
        )
        tomorrow_rows, tomorrow_meta = self._score_expected_return_strategy(
            "tomorrow_picks",
            candidates,
            top_n,
            market,
            market_regime,
            apply_deepseek=apply_deepseek,
        )
        swing_rows, swing_meta = self._score_expected_return_strategy(
            "swing_picks",
            candidates,
            top_n,
            market,
            market_regime,
            apply_deepseek=apply_deepseek,
        )

        self._apply_validation_gates(tomorrow_rows, tomorrow_meta, swing_rows, swing_meta)
        recommendations_by_horizon["tomorrow_picks"] = tomorrow_rows
        recommendations_by_horizon["swing_picks"] = swing_rows

        deepseek_meta_by_strategy = self._apply_deepseek_after_gate(
            recommendations_by_horizon,
            market,
            apply_deepseek=apply_deepseek,
        )
        short_deepseek_meta = deepseek_meta_by_strategy.get("today_term", skipped_deepseek_meta("today_term"))
        tomorrow_deepseek_meta = deepseek_meta_by_strategy.get(
            "tomorrow_picks", skipped_deepseek_meta("tomorrow_picks")
        )
        swing_deepseek_meta = deepseek_meta_by_strategy.get("swing_picks", skipped_deepseek_meta("swing_picks"))

        tomorrow_rows = recommendations_by_horizon.get("tomorrow_picks", tomorrow_rows)
        swing_rows = recommendations_by_horizon.get("swing_picks", swing_rows)
        finalize_deepseek_meta(short_meta, recommendations_by_horizon.get("today_term", []), short_deepseek_meta)
        finalize_deepseek_meta(tomorrow_meta, tomorrow_rows, tomorrow_deepseek_meta)
        finalize_deepseek_meta(swing_meta, swing_rows, swing_deepseek_meta)

        strategy_metas = {
            "today_term": short_meta,
            "tomorrow_picks": tomorrow_meta,
            "swing_picks": swing_meta,
        }
        for strategy_name, strategy_rows in recommendations_by_horizon.items():
            strategy_metas[strategy_name]["market_regime"] = market_regime
            attach_generation_provenance(strategy_metas[strategy_name], strategy_name, strategy_rows, candidates)
        recommendations_by_horizon["long_term_watch"] = LongTermWatchScorer().score(
            recommendations_by_horizon,
            candidates,
            top_n,
            validation_store=self.validation_store if apply_deepseek else None,
        )
        long_term_rows = recommendations_by_horizon["long_term_watch"]
        if not apply_deepseek:
            long_term_rows = merge_and_rank_rows(long_term_rows, "long_term_watch")
            recommendations_by_horizon["long_term_watch"] = long_term_rows
        long_term_meta = (
            _persisted_feature_meta("long_term_watch", long_term_rows, self.validation_store)
            if apply_deepseek
            else skipped_deepseek_meta("long_term_watch")
        )
        deepseek_meta_by_strategy["long_term_watch"] = long_term_meta
        return (
            recommendations_by_horizon,
            short_meta,
            {
                "today_term": short_deepseek_meta,
                "tomorrow_picks": tomorrow_deepseek_meta,
                "swing_picks": swing_deepseek_meta,
                "long_term_watch": long_term_meta,
            },
        )

    def _score_expected_return_strategy(
        self,
        strategy_name: str,
        candidates: pd.DataFrame,
        top_n: int,
        market: str,
        market_regime: Dict[str, object],
        *,
        apply_deepseek: bool = True,
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        context = expected_return_ranking_context(
            strategy_name,
            validation_store=self.validation_store,
            top_k=max(1, min(10, int(top_n or 10))),
        )
        kwargs = {
            "top_n": top_n,
            "market_filter": market,
            "market_regime": market_regime,
            "expected_return_samples": context["samples"],
            "use_expected_return_ranking": context["use_ranking"],
        }
        if apply_deepseek:
            kwargs["scoring_context"] = _deepseek_scoring_context(strategy_name, self.validation_store)
        if strategy_name == "tomorrow_picks":
            rows, meta = score_tomorrow_picks(candidates, **kwargs)
        elif strategy_name == "swing_picks":
            rows, meta = score_swing_2_5d_picks(candidates, **kwargs)
        else:
            raise ValueError(f"Unsupported strategy: {strategy_name}")
        meta["expected_return_ranking"] = context["meta"]
        return rows, meta

    def _apply_validation_gates(
        self,
        tomorrow_rows: List[Dict[str, object]],
        tomorrow_meta: Dict[str, object],
        swing_rows: List[Dict[str, object]],
        swing_meta: Dict[str, object],
    ) -> None:
        try:
            apply_tomorrow_validation_gate(
                tomorrow_rows,
                tomorrow_meta,
                self.cached_metrics_fn("tomorrow_picks", validation_gate_window_days()),
            )
        except Exception as exc:
            reason = "验证指标读取失败，暂停重点观察并仅保留备选：{}".format(exc)
            tomorrow_meta["validation_gate"] = {
                "state": "unavailable",
                "blocked": True,
                "allows_backup": True,
                "reason": reason,
            }
            demote_tomorrow_rows_to_backup(tomorrow_rows, tomorrow_meta, reason)
        _apply_validation_gate_safe("swing_picks", swing_rows, swing_meta, self.cached_metrics_fn)

    def _apply_deepseek_after_gate(
        self,
        recommendations_by_horizon: Dict[str, List[Dict[str, object]]],
        market: str,
        *,
        apply_deepseek: bool,
    ) -> Dict[str, Dict[str, object]]:
        if not apply_deepseek:
            meta = {}
            for strategy, rows in recommendations_by_horizon.items():
                recommendations_by_horizon[strategy] = merge_and_rank_rows(rows or [], strategy)
                meta[strategy] = skipped_deepseek_meta(
                    strategy,
                    status="disabled",
                    reason="Persisted DeepSeek feature attachment was disabled for this request.",
                )
            return meta
        meta = {}
        for strategy, rows in recommendations_by_horizon.items():
            source_rows = list(rows or [])
            if not source_rows:
                meta[strategy] = {"enabled": False, "status": "local_only", "strategy": strategy}
            else:
                attached = merge_and_rank_rows(source_rows, strategy)
                recommendations_by_horizon[strategy] = attached
                meta[strategy] = _persisted_feature_meta(strategy, attached, self.validation_store)
        return meta


def _apply_validation_gate_safe(
    strategy_name: str,
    rows: List[Dict[str, object]],
    meta: Dict[str, object],
    cached_metrics_fn,
) -> Dict[str, object]:
    try:
        metrics = cached_metrics_fn(strategy_name, validation_gate_window_days())
        if strategy_name == "tomorrow_picks":
            return apply_tomorrow_validation_gate(rows, meta, metrics)
        return apply_strategy_validation_gate(strategy_name, rows, meta, metrics)
    except Exception as exc:
        reason = "验证指标读取失败，暂停执行并仅保留备选：{}".format(exc)
        meta["validation_gate"] = {
            "state": "unavailable",
            "blocked": True,
            "allows_backup": True,
            "reason": reason,
        }
        if strategy_name == "tomorrow_picks":
            demote_tomorrow_rows_to_backup(rows, meta, reason)
        else:
            demote_strategy_rows_to_backup(strategy_name, rows, meta, reason)
        return meta["validation_gate"]


def finalize_recommendation_payload_meta(
    short_rows: List[Dict[str, object]],
    meta: Dict[str, object],
    blacklist_payload: Dict[str, object],
    hard_filter_report: Dict[str, object],
    market_regime: Dict[str, object],
    deepseek_meta_by_strategy: Dict[str, object],
    top_n: int,
    stability_update_fn,
    validation_store,
    cached_metrics_fn,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    short_stability = stability_update_fn("today_term", short_rows)
    base_theme_cap = int(getattr(config, "RECOMMENDATION_MAX_DISPLAY_PER_THEME", 3))
    theme_cap = regime_aware_display_theme_cap(market_regime, base_theme_cap)
    short_display_rows, short_theme_limited = limit_theme_concentration(short_stability["rows"], top_n, theme_cap)
    attach_validation_summary(short_display_rows, validation_store, "today_term", metrics_fn=cached_metrics_fn)
    meta["top_n"] = top_n
    meta["risk_blacklist"] = risk_blacklist_summary(blacklist_payload)
    meta["hard_filter_report"] = hard_filter_report
    meta["stability"] = {
        "today_term": {
            "new_entries": short_stability["new_entries"],
            "dropped": short_stability["dropped"],
            "retained": short_stability["retained"],
            "last_updated": short_stability["last_updated"],
        },
    }
    by_strategy = {
        strategy: _public_deepseek_meta(item) for strategy, item in (deepseek_meta_by_strategy or {}).items()
    }
    budget = usage_summary(validation_store)
    strategy_rows = list(by_strategy.values())
    requested = sum(int(item.get("requested") or 0) for item in strategy_rows)
    reviewed = sum(int(item.get("reviewed") or 0) for item in strategy_rows)
    production_applied = any(bool(item.get("production_applied")) for item in strategy_rows)
    errors = [item for item in strategy_rows if _deepseek_item_status(item) == "error"]
    overall_status = _overall_deepseek_status(strategy_rows, production_applied=production_applied)
    meta["deepseek"] = {
        "enabled": bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True)),
        "production_applied": production_applied,
        "weight": 0.25,
        **budget,
        "status": overall_status,
        "requested": requested,
        "reviewed": reviewed,
        "coverage_pct": round(reviewed * 100.0 / max(1, requested), 2),
        "abstain_count": sum(int(item.get("abstain_count") or 0) for item in strategy_rows),
        "by_strategy": by_strategy,
        "today_phase": today_phase(),
        "error_type": str((errors[-1] if errors else {}).get("error_type") or budget.get("error_type") or ""),
        "error_message": str((errors[-1] if errors else {}).get("error_message") or budget.get("error_message") or ""),
    }
    meta["market_regime"] = market_regime
    meta["display_theme_cap"] = theme_cap
    meta["base_display_theme_cap"] = base_theme_cap
    meta["display_theme_limited"] = {
        "today_term": short_theme_limited,
    }
    meta["display_count"] = len(short_display_rows)
    return short_display_rows, meta


def regime_aware_display_theme_cap(market_regime: Dict[str, object], base_cap: int = None) -> int:
    base = max(1, int(base_cap or getattr(config, "RECOMMENDATION_MAX_DISPLAY_PER_THEME", 3)))
    if not bool(getattr(config, "ENABLE_REGIME_THEME_CAP", False)):
        return base
    level = str((market_regime or {}).get("level") or "").strip().lower()
    score = coerce_number((market_regime or {}).get("score"), None)
    if level == "risk_off" or (score is not None and score <= 42):
        delta = max(0, int(getattr(config, "RECOMMENDATION_THEME_CAP_RISK_OFF_DELTA", 1)))
        return max(1, base - delta)
    return base


def _public_deepseek_meta(deepseek_meta: Dict[str, object]) -> Dict[str, object]:
    item = dict(deepseek_meta or {})
    item.pop("filtered_rows", None)
    return item

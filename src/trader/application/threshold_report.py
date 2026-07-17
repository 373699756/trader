"""Deterministic threshold pre-registration report from frozen replay inputs."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

from trader.application.recommendations import RecommendationEngine
from trader.domain.models import FusionMode, RecommendationSnapshot, Strategy


def build_threshold_report(snapshots: Sequence[RecommendationSnapshot]) -> dict[str, object]:
    if not snapshots:
        raise ValueError("threshold report requires at least one frozen snapshot")
    grouped: dict[Strategy, list[RecommendationSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        if not snapshot.frozen:
            raise ValueError("threshold report only accepts frozen snapshots")
        if snapshot.strategy is Strategy.LONG:
            raise ValueError("long snapshots do not participate in threshold pre-registration")
        grouped[snapshot.strategy].append(snapshot)

    reports: dict[str, object] = {}
    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
        strategy_snapshots = sorted(grouped.get(strategy, ()), key=lambda item: (item.published_at, item.snapshot_id))
        if not strategy_snapshots:
            continue
        strategy_versions = {snapshot.strategy_version for snapshot in strategy_snapshots}
        if len(strategy_versions) != 1:
            raise ValueError(f"mixed strategy versions for {strategy.value}")
        fusion_versions = {snapshot.fusion_version for snapshot in strategy_snapshots}
        if len(fusion_versions) != 1:
            raise ValueError(f"mixed fusion versions for {strategy.value}")
        candidates_by_snapshot = [RecommendationEngine.replay_candidates(snapshot) for snapshot in strategy_snapshots]
        candidates = tuple(item for rows in candidates_by_snapshot for item in rows)
        recommendation_counts = [len(snapshot.recommendations) for snapshot in strategy_snapshots]
        coverage_values = [item.score.confidence_coverage for item in candidates]
        reports[strategy.value] = {
            "strategy_version": next(iter(strategy_versions)),
            "fusion_version": next(iter(fusion_versions)),
            "snapshot_count": len(strategy_snapshots),
            "candidate_count": len(candidates),
            "score_distribution": _distribution([item.score.final_score for item in candidates]),
            "recommendation_count": _count_summary(recommendation_counts),
            "empty_recommendation_ratio": _ratio(
                sum(count == 0 for count in recommendation_counts), len(strategy_snapshots)
            ),
            "topk_change": _topk_change(strategy_snapshots),
            "deepseek_coverage": {
                "mean_confidence_coverage": _mean(coverage_values),
                "applied_ratio": _ratio(sum(item.score.fusion_applied for item in candidates), len(candidates)),
            },
            "local_degraded_ratio": _ratio(
                sum(snapshot.fusion_mode is FusionMode.LOCAL_DEGRADED for snapshot in strategy_snapshots),
                len(strategy_snapshots),
            ),
            "risk_block_rate": _ratio(
                sum(item.veto or item.action_reason == "risk_veto" for item in candidates), len(candidates)
            ),
            "snapshot_ids": [snapshot.snapshot_id for snapshot in strategy_snapshots],
        }
    return {"schema_version": "threshold_report_v1", "strategies": reports}


def _distribution(values: Sequence[float]) -> dict[str, object]:
    finite = sorted(float(value) for value in values if math.isfinite(value))
    if not finite:
        return {"count": 0, "minimum": None, "p25": None, "median": None, "p75": None, "maximum": None}
    return {
        "count": len(finite),
        "minimum": finite[0],
        "p25": _quantile(finite, 0.25),
        "median": _quantile(finite, 0.50),
        "p75": _quantile(finite, 0.75),
        "maximum": finite[-1],
    }


def _count_summary(values: Sequence[int]) -> dict[str, object]:
    return {
        "total": sum(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": _mean(values),
    }


def _topk_change(snapshots: Sequence[RecommendationSnapshot]) -> dict[str, object]:
    distances: list[float] = []
    for previous, current in zip(snapshots, snapshots[1:], strict=False):
        previous_codes = {item.features.quote.code for item in previous.recommendations}
        current_codes = {item.features.quote.code for item in current.recommendations}
        union = previous_codes | current_codes
        distances.append(0.0 if not union else 1.0 - len(previous_codes & current_codes) / len(union))
    return {
        "comparison_count": len(distances),
        "mean_jaccard_distance": _mean(distances) if distances else None,
    }


def _quantile(values: Sequence[float], quantile: float) -> float:
    position = quantile * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return _rounded(values[lower] * (1.0 - fraction) + values[upper] * fraction)


def _mean(values: Sequence[float] | Sequence[int]) -> float | None:
    return _rounded(sum(values) / len(values)) if values else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return _rounded(numerator / denominator) if denominator else None


def _rounded(value: float) -> float:
    return round(value, 4)


__all__ = ["build_threshold_report"]

from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES, PipelineStageSnapshot, PipelineStageStatus, RecommendationPipelineStatus, SourceHealth, SourceHealthState, StageState
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.freeze_identity import FreezeItem, FreezeQuote, ScoredFreeze
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")

NOW = datetime(2026, 8, 11, 14, 40, tzinfo=SHANGHAI)


def pipeline() -> RecommendationPipelineStatus:
    keys = (
        "local_data",
        "static_filter",
        "dynamic_market",
        "dynamic_filter",
        "market_features",
        "model_filter",
        "local_score",
        "local_risk",
        "fixed_selection",
        "risk_review",
        "action_publish",
    )
    return RecommendationPipelineStatus(
        "action_publish",
        tuple(PipelineStageStatus(key, "completed", 1, 1) for key in keys),
    )


def freeze(
    strategy: Strategy = Strategy.TOMORROW,
    *,
    sequence: int = 1,
    stage: str = "local",
    parent_version: str | None = None,
    score: float = 88.0,
) -> ScoredFreeze:
    return ScoredFreeze(
        strategy=strategy,
        trade_date=NOW.date(),
        sequence=sequence,
        observed_at=NOW,
        stage=stage,
        parent_version=parent_version,
        input_versions=(("market", "market-initial"), ("daily", "daily-current")),
        config_version="config-current",
        strategy_version="strategy-current",
        fusion_version="fusion-current",
        items=(
            FreezeItem(
                code="600001",
                action=RecommendationAction.EXECUTABLE,
                selected=True,
                rank=1,
                candidate_score=84.0,
                local_score=score if stage == "local" else 88.0,
                final_score=score,
                score_components=(("trend", 88.0),),
                risk_codes=(),
                reason="selected",
                board=Board.MAIN,
                selection_rank=1,
                quote=FreezeQuote(
                    "600001",
                    10.5,
                    1.2,
                    120_000_000.0,
                    2.1,
                    12_000_000_000.0,
                    "tencent",
                    NOW - timedelta(minutes=5),
                    "quote-fixture",
                ),
            ),
        ),
        filter_aggregates=(("st_or_delisting", 2),),
    )


def stage_snapshots(observed_at: datetime = NOW, *, count: int = 1) -> tuple[PipelineStageSnapshot, ...]:
    return tuple(
        PipelineStageSnapshot(
            stage,
            index + 1,
            f"batch-{index}",
            f"batch-{index + 1}",
            observed_at,
            StageState.READY,
            count,
            count,
            0,
            0,
            0,
            (),
            SourceHealth(SourceHealthState.READY, 1, 1, observed_at, 0),
            10,
            False,
        )
        for index, stage in enumerate(PIPELINE_STAGES)
    )

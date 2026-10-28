"""Read-only, fixed local inputs for real supplier comparisons."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo

from trader.download.infra.current_industry_publication import CurrentIndustryCache
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.market_data.history.history import BuildHistoryContext, DailyBar, HistoryContext
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import (
    ProductionModelScoringService,
    SharedModelFeatureCache,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelPredictorPort
from trader.recommendation.domain.market.industry import QuoteWithIndustry
from trader.recommendation.domain.market.models import FeatureSnapshot, ModelIndustryReference
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.market_data.published_history_bars import QfqBars
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.policy_settings import _recommendation_policy
from trader.recommendation.infra.scoring.cost_settings import LoadExecutionCostPolicy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.infra.training_settings import LoadProfileTrainingSettings


@dataclass(frozen=True)
class FixedReferences:
    observations: tuple[SourceObservation, ...]
    model_industries: Mapping[str, ModelIndustryReference]
    open_dates: tuple[date, ...]
    versions: tuple[str, ...]
    industry_read_ms: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_industries", MappingProxyType(dict(self.model_industries)))


@dataclass(frozen=True)
class ActiveModelHead:
    strategy: Strategy
    feature_ids: tuple[str, ...]
    requires_industry: bool

    def __post_init__(self) -> None:
        if not self.feature_ids or len(set(self.feature_ids)) != len(self.feature_ids):
            raise ValueError("active model feature identities are invalid")


@dataclass(frozen=True)
class FactorInputs:
    profile: ScoringProfileId
    policy: RecommendationPolicy
    builder: FeatureBuilder
    models: ModelScoringRouter
    model_heads: tuple[ActiveModelHead, ...]
    histories: Mapping[str, tuple[DailyBar, ...]]
    contexts: Mapping[str, HistoryContext]
    snapshot_hash: str
    config_version: str
    preparation_ms: Mapping[str, float]
    history_sessions: int


def ReadReferences(reference_root: Path, at: datetime) -> FixedReferences:
    """Read the same fixed publications as recommendation, without supplier clients."""
    started = time.monotonic()
    publication = SQLiteReferenceIndex(reference_root / "company_st.sqlite3")
    references = PublishedReferences(
        publication.read,
        publication.identity,
        CurrentIndustryCache(reference_root / "industry.sqlite3").load,
        max_age_seconds=30 * 86400,
    )
    references.initialize()
    reference = references.reference()
    if reference is None or reference.checked_at > at:
        raise RuntimeError("published_reference_unavailable")
    observations = references.security_observations()
    return FixedReferences(
        observations,
        references.model_industries(tuple(item.subject_key for item in observations)),
        reference.open_dates,
        tuple(sorted(references.versions().values())),
        (time.monotonic() - started) * 1000,
    )


def LoadFactorInputs(
    config: Path, qfq_database: Path, profile: ScoringProfileId, codes: tuple[str, ...], at: datetime
) -> FactorInputs:
    started = time.monotonic()
    settings = LoadRuntimeSettings(config)
    strategy = LoadStrategySettings(settings.strategy_config_path, scoring_profile=profile)
    training = LoadProfileTrainingSettings(settings.project_root, profile)
    cache = SQLiteQfqWindowCache(qfq_database, profile)
    manifest = cache.manifest()
    if manifest is None:
        raise RuntimeError("qfq_snapshot_unavailable")
    windows = cache.read_codes(codes)
    histories = {window.code: HistoryAtCut(QfqBars(window), at) for window in windows}
    verified = cache.manifest()
    if verified is None or verified.snapshot_hash != manifest.snapshot_hash:
        raise RuntimeError("qfq_snapshot_changed")
    read_ms = (time.monotonic() - started) * 1000
    started = time.monotonic()
    loaded = LoadScoringProfile(profile, training_root=settings.data_paths.models)
    model_heads = tuple(
        ActiveModelHead(
            strategy=target,
            feature_ids=cast(ModelPredictorPort, loaded.heads[target].predictor).feature_ids,
            requires_industry=cast(
                ModelPredictorPort, loaded.heads[target].predictor
            ).exposure_contract.requires_industry,
        )
        for target in (Strategy.TOMORROW, Strategy.D25)
    )
    cost = LoadExecutionCostPolicy(config.parent)
    shared = SharedModelFeatureCache()
    models = ModelScoringRouter(
        profile,
        {
            target: ProductionModelScoringService(loaded, target, cost_policy=cost, shared_features=shared)
            for target in (Strategy.TOMORROW, Strategy.D25)
        },
    )
    model_ms = (time.monotonic() - started) * 1000
    history_sessions = max(models.history_required_sessions(target) for target in (Strategy.TOMORROW, Strategy.D25))
    started = time.monotonic()
    contexts = {code: BuildHistoryContext(bars, lookback_sessions=history_sessions) for code, bars in histories.items()}
    context_ms = (time.monotonic() - started) * 1000
    builder = FeatureBuilder(
        strategy.news_signal,
        strategy.tomorrow_tail_signal,
        strategy.market_regime,
        strategy.long_research,
        strategy.feature_component_weights,
        model_momentum_horizons=training.momentum_horizons,
    )
    return FactorInputs(
        profile=profile,
        policy=_recommendation_policy(strategy),
        builder=builder,
        models=models,
        model_heads=model_heads,
        histories=MappingProxyType(histories),
        contexts=MappingProxyType(contexts),
        snapshot_hash=manifest.snapshot_hash,
        config_version=settings.config_version + ":" + strategy.strategy_version,
        preparation_ms=MappingProxyType(
            {"qfq_read_validate": read_ms, "history_context_build": context_ms, "model_load": model_ms}
        ),
        history_sessions=history_sessions,
    )


def AttachModelIndustries(
    features: tuple[FeatureSnapshot, ...], references: FixedReferences
) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        replace(
            item,
            quote=QuoteWithIndustry(
                item.quote, references.model_industries.get(item.quote.code), item.observed_at.date()
            ),
            model_industry=references.model_industries.get(item.quote.code),
        )
        for item in features
    )


def HistoryAtCut(bars: tuple[DailyBar, ...], at: datetime) -> tuple[DailyBar, ...]:
    """Match published-history admission: today's completed bar is usable after 15:00."""
    local = at.astimezone(ZoneInfo("Asia/Shanghai"))
    today = local.date().isoformat()
    return tuple(bar for bar in bars if bar.trade_date < today or (bar.trade_date == today and local.hour >= 15))


__all__ = [
    "ActiveModelHead",
    "FixedReferences",
    "FactorInputs",
    "ReadReferences",
    "LoadFactorInputs",
    "AttachModelIndustries",
    "HistoryAtCut",
]

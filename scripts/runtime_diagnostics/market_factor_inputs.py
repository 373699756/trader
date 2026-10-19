"""Read-only, fixed local inputs for real supplier comparisons."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo

from trader.bootstrap import BuildIndustrySource
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.market_data.history.history import BuildHistoryContext, DailyBar, HistoryContext
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import (
    ProductionModelScoringService,
    SharedModelFeatureCache,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.data_plane_records import SecurityMasterRecord
from trader.recommendation.application.ports.loaded_profile import ModelPredictorPort
from trader.recommendation.domain.market.industry import QuoteWithIndustry
from trader.recommendation.domain.market.models import FeatureSnapshot, ModelIndustryReference
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.market_data.local_industry_source import LocalIndustrySource
from trader.recommendation.infra.market_data.published_history_bars import QfqBars
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.persistence.data_plane_codec import RowToRecord
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


def ReadReferences(database: Path, at: datetime, industry_database: Path, calendar_path: Path) -> FixedReferences:
    """Do not use SQLiteDataPlane.load: it initializes its active database."""
    observations: list[SourceObservation] = []
    industries: dict[str, ModelIndustryReference] = {}
    if database.is_file():
        with closing(
            OpenSqliteConnection(database, SqliteConnectionOptions(read_only=True, timeout_seconds=5))
        ) as connection:
            for row in connection.run("SELECT * FROM security_master_recent WHERE status='committed'"):
                record = RowToRecord("security_master_recent", row)
                if not isinstance(record, SecurityMasterRecord):
                    raise TypeError("unexpected reference record")
                if record.observed_at > at or record.source_time > at:
                    continue
                fields = {
                    key: value
                    for key, value in record.payload.items()
                    if value is None or isinstance(value, (str, int, float, bool))
                }
                observations.append(
                    SourceObservation(
                        source=record.source,
                        subject_key=record.code,
                        observed_at=record.observed_at,
                        source_time=record.source_time,
                        received_at=record.observed_at,
                        effective_at=record.observed_at,
                        data_version=record.data_version,
                        fields=fields,
                        missing_reasons={},
                        payload_hash=record.payload_hash,
                        status="success",
                        error_code=None,
                    )
                )
    industry_started = time.monotonic()
    if industry_database is not None:
        for item in LocalIndustrySource(industry_database, BuildIndustrySource()).load_cached(at):
            industry_fields = item.fields
            industries[item.subject_key] = ModelIndustryReference(
                str(industry_fields["model_industry"]),
                str(industry_fields["model_industry_classification"]),
                date.fromisoformat(str(industry_fields["model_industry_effective_date"])),
                str(industry_fields["model_industry_source"]),
                str(industry_fields["model_industry_data_version"]),
            )
    calendar = json.loads(calendar_path.read_text()) if calendar_path.is_file() else {}
    open_dates = tuple(sorted(date.fromisoformat(item) for item in calendar.get("dates", [])))
    return FixedReferences(
        tuple(observations),
        MappingProxyType(industries),
        open_dates,
        tuple(sorted({item.data_version for item in observations})),
        (time.monotonic() - industry_started) * 1000 if industry_database is not None else 0.0,
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

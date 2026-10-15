"""Read-only, fixed local inputs for real supplier comparisons."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.market_data.history.history import DailyBar, HistoryContext, build_history_context
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import load_runtime_settings, load_strategy_settings
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import (
    ProductionModelScoringService,
    SharedModelFeatureCache,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.data_plane_records import SecurityMasterRecord
from trader.recommendation.domain.market.models import FeatureSnapshot, ModelIndustryReference
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.market_data.published_history_bars import qfq_bars
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.infra.persistence.data_plane_codec import row_to_record
from trader.recommendation.infra.policy_settings import _recommendation_policy
from trader.recommendation.infra.scoring.cost_settings import load_execution_cost_policy
from trader.recommendation.infra.scoring.profile_factory import load_scoring_profile
from trader.training.infra.training_settings import load_profile_training_settings


@dataclass(frozen=True)
class FixedReferences:
    observations: tuple[SourceObservation, ...]
    model_industries: Mapping[str, ModelIndustryReference]
    open_dates: tuple[date, ...]
    versions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_industries", MappingProxyType(dict(self.model_industries)))


@dataclass(frozen=True)
class FactorInputs:
    profile: ScoringProfileId
    policy: RecommendationPolicy
    builder: FeatureBuilder
    models: ModelScoringRouter
    histories: Mapping[str, tuple[DailyBar, ...]]
    contexts: Mapping[str, HistoryContext]
    snapshot_hash: str
    config_version: str
    preparation_ms: Mapping[str, float]
    history_sessions: int


def read_references(runtime_dir: Path, at: datetime) -> FixedReferences:
    """Do not use SQLiteDataPlane.load: it initializes its active database."""
    database = runtime_dir / "market-data/market-data.sqlite3"
    observations: list[SourceObservation] = []
    industries: dict[str, ModelIndustryReference] = {}
    if database.is_file():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute("SELECT * FROM security_master_recent WHERE status='committed'"):
                record = row_to_record("security_master_recent", row)
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
                names = (
                    "model_industry",
                    "model_industry_classification",
                    "model_industry_effective_date",
                    "model_industry_source",
                    "model_industry_data_version",
                )
                values = tuple(record.payload.get(key) for key in names)
                if all(isinstance(value, str) and value for value in values):
                    industry, classification, effective, source, version = (str(value) for value in values)
                    effective_date = date.fromisoformat(effective)
                    if effective_date <= at.date():
                        industries[record.code] = ModelIndustryReference(
                            industry, classification, effective_date, source, version
                        )
    calendar_path = runtime_dir / "calendar.json"
    calendar = json.loads(calendar_path.read_text()) if calendar_path.is_file() else {}
    open_dates = tuple(sorted(date.fromisoformat(item) for item in calendar.get("dates", [])))
    return FixedReferences(
        tuple(observations),
        MappingProxyType(industries),
        open_dates,
        tuple(sorted({item.data_version for item in observations})),
    )


def load_factor_inputs(
    config: Path, qfq_root: Path, profile: ScoringProfileId, codes: tuple[str, ...], at: datetime
) -> FactorInputs:
    started = time.monotonic()
    settings = load_runtime_settings(config)
    strategy = load_strategy_settings(settings.strategy_config_path, scoring_profile=profile)
    training = load_profile_training_settings(settings.project_root, profile)
    cache = SQLiteQfqWindowCache(qfq_root, profile)
    manifest = cache.manifest()
    if manifest is None:
        raise RuntimeError("qfq_snapshot_unavailable")
    windows = cache.read_codes(codes)
    histories = {window.code: history_at_cut(qfq_bars(window), at) for window in windows}
    verified = cache.manifest()
    if verified is None or verified.snapshot_hash != manifest.snapshot_hash:
        raise RuntimeError("qfq_snapshot_changed")
    read_ms = (time.monotonic() - started) * 1000
    started = time.monotonic()
    loaded = load_scoring_profile(profile, training_root=settings.project_root / "data/train")
    cost = load_execution_cost_policy(config.parent)
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
    contexts = {
        code: build_history_context(bars, lookback_sessions=history_sessions) for code, bars in histories.items()
    }
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
        profile,
        _recommendation_policy(strategy),
        builder,
        models,
        MappingProxyType(histories),
        MappingProxyType(contexts),
        manifest.snapshot_hash,
        settings.config_version + ":" + strategy.strategy_version,
        MappingProxyType({"qfq_read_validate": read_ms, "history_context_build": context_ms, "model_load": model_ms}),
        history_sessions,
    )


def attach_model_industries(
    features: tuple[FeatureSnapshot, ...], references: FixedReferences
) -> tuple[FeatureSnapshot, ...]:
    return tuple(replace(item, model_industry=references.model_industries.get(item.quote.code)) for item in features)


def history_at_cut(bars: tuple[DailyBar, ...], at: datetime) -> tuple[DailyBar, ...]:
    """Match published-history admission: today's completed bar is usable after 15:00."""
    local = at.astimezone(ZoneInfo("Asia/Shanghai"))
    today = local.date().isoformat()
    return tuple(bar for bar in bars if bar.trade_date < today or (bar.trade_date == today and local.hour >= 15))


__all__ = [
    "FixedReferences",
    "FactorInputs",
    "read_references",
    "load_factor_inputs",
    "attach_model_industries",
    "history_at_cut",
]

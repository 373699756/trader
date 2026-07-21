"""Immutable validated configuration models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trader.application.cache import CachePolicy
from trader.domain.news import NewsSignalPolicy
from trader.domain.research import D25SignalPolicy, LongResearchPolicy
from trader.domain.tail import TailSignalPolicy


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    debug: bool
    use_reloader: bool
    allow_insecure_non_loopback: bool


@dataclass(frozen=True)
class PipelineSettings:
    event_queue_size: int
    priority_queue_size: int
    market_workers: int
    normalization_workers: int
    strategy_workers: int
    deepseek_workers: int
    shutdown_timeout_seconds: float
    cadence_seconds: Mapping[str, Mapping[str, float]]
    publish_heartbeat_seconds: int


@dataclass(frozen=True)
class MarketDataSettings:
    eastmoney_timeout_seconds: float
    candidate_timeout_seconds: float
    history_timeout_seconds: float
    research_timeout_seconds: float
    minimum_market_rows: int
    candidate_pool_size: int
    single_flight: bool
    circuit_breaker_failures: int
    circuit_breaker_seconds: int
    source_contract_versions: Mapping[str, str]
    tushare: TushareSettings
    cache_policy: CachePolicy


@dataclass(frozen=True)
class TushareSettings:
    enabled: bool
    timeout_seconds: float
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class PerformanceWorkloadSettings:
    market_rows: int
    candidate_rows: int


@dataclass(frozen=True)
class PerformanceRoundSettings:
    warmup: int
    measurement: int


@dataclass(frozen=True)
class PerformanceMemorySettings:
    cache_total_bytes: int
    growth_percent: float


@dataclass(frozen=True)
class PerformanceBudgetSettings:
    schema_version: int
    workload: PerformanceWorkloadSettings
    rounds: PerformanceRoundSettings
    latency_p95_ms: Mapping[str, float]
    data_age_p95_seconds: Mapping[str, float]
    memory: PerformanceMemorySettings
    relative_regression_percent: float


@dataclass(frozen=True)
class DeepSeekSettings:
    enabled: bool
    base_url: str
    model: str
    challenger_model: str
    challenger_limits: Mapping[str, int]
    timeout_seconds: float
    batch_size: int
    max_tokens: int
    daily_hard_limit: int
    strategy_limits: Mapping[str, int]
    stage_targets: Mapping[str, int]
    stage_limits: Mapping[str, int]
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class ApiSettings:
    default_top_n: int
    maximum_top_n: int
    event_page_limit: int
    maximum_event_page_limit: int
    sse_history_size: int
    sse_client_queue_size: int
    sse_max_clients: int


@dataclass(frozen=True)
class RuntimeSettings:
    schema_version: int
    config_version: str
    config_path: Path
    project_root: Path
    runtime_dir: Path
    strategy_config_path: Path
    long_watchlist_path: Path
    server: ServerSettings
    pipeline: PipelineSettings
    market_data: MarketDataSettings
    performance_budgets: PerformanceBudgetSettings
    deepseek: DeepSeekSettings
    api: ApiSettings


@dataclass(frozen=True)
class FusionSettings:
    version: str
    local_weight: float
    deepseek_weight: float
    confidence_coverage_min: float
    minimum_known_dimensions: int
    local_risk_cap: float
    deepseek_risk_cap: float
    score_decimals: int
    rounding: str


@dataclass(frozen=True)
class SelectionSettings:
    default_top_k: int
    maximum_top_k: int
    maximum_per_industry: int
    observation_margin: float
    thresholds: Mapping[str, float]


@dataclass(frozen=True)
class HardFilterSettings:
    blacklist_codes: tuple[str, ...]
    structured_risk_thresholds: Mapping[str, float]


@dataclass(frozen=True)
class RiskRuleSettings:
    risk_code: str
    severity: str
    penalty: float
    minimum_confidence: float
    evidence_ttl_hours: int
    group: str
    veto: bool
    allowed_evidence_types: tuple[str, ...]
    strategies: tuple[str, ...]
    trigger_factor: str
    trigger_operator: str
    trigger_thresholds: tuple[float, ...]
    combination_mode: str
    risk_fact_id_fields: tuple[str, ...]
    local_trigger_enabled: bool


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    strategies: tuple[str, ...]
    raw_inputs: tuple[str, ...]
    formula: str
    unit: str
    direction: str
    observation_time: str
    adjustment: str
    lookback_window: int
    minimum_samples: int
    winsor_enabled: bool
    winsor_lower_quantile: float
    winsor_upper_quantile: float
    normalization: str
    missing_policy: str
    output_range: tuple[float, float]
    version: str


@dataclass(frozen=True)
class StrategySettings:
    schema_version: int
    strategy_version: str
    fusion: FusionSettings
    selection: SelectionSettings
    candidate_weights: Mapping[str, float]
    hard_filters: HardFilterSettings
    today_news_signal: NewsSignalPolicy
    tomorrow_tail_signal: TailSignalPolicy
    d25_signal: D25SignalPolicy
    long_research: LongResearchPolicy
    dimension_weights: Mapping[str, Mapping[str, float]]
    local_strategy_weights: Mapping[str, Mapping[str, float]]
    risk_rules: tuple[RiskRuleSettings, ...]
    factor_contract: Mapping[str, object]
    factor_registry: Mapping[str, FactorDefinition]


@dataclass(frozen=True)
class LongWatchItem:
    code: str
    name: str
    industry: str
    target_price: float | None


@dataclass(frozen=True)
class LongWatchlist:
    schema_version: int
    watchlist_version: str
    items: tuple[LongWatchItem, ...]

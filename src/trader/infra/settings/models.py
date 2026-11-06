"""Immutable validated configuration models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from trader.infra.cache_contracts import CachePolicy
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.disk_types import DiskKind, DiskMedium


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    debug: bool
    use_reloader: bool
    allow_insecure_non_loopback: bool


@dataclass(frozen=True)
class HistoryPreloadSettings:
    warn_after_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.warn_after_seconds, bool)
            or not isinstance(self.warn_after_seconds, (int, float))
            or not isfinite(self.warn_after_seconds)
            or not 0.001 <= self.warn_after_seconds <= 3600
        ):
            raise ValueError("history preload settings are invalid")


@dataclass(frozen=True)
class CompanyPreloadSettings:
    timeout_seconds: float
    publication_attempts: int

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) not in {int, float}
            or not isfinite(self.timeout_seconds)
            or not 0.001 <= self.timeout_seconds <= 3600
            or type(self.publication_attempts) is not int
            or not 1 <= self.publication_attempts <= 10
        ):
            raise ValueError("company preload settings are invalid")


@dataclass(frozen=True)
class ExecutorSettings:
    workers: int
    queue_capacity: int

    def __post_init__(self) -> None:
        if type(self.workers) is not int or self.workers < 1:
            raise ValueError("executor workers must be a positive integer")
        if type(self.queue_capacity) is not int or self.queue_capacity < 0:
            raise ValueError("executor queue capacity must be a non-negative integer")


@dataclass(frozen=True)
class PriorityExecutorSettings:
    normal_workers: int
    urgent_workers: int
    queue_capacity: int

    def __post_init__(self) -> None:
        ExecutorSettings(self.normal_workers, self.queue_capacity)
        if type(self.urgent_workers) is not int or self.urgent_workers < 0:
            raise ValueError("executor urgent workers must be a non-negative integer")


@dataclass(frozen=True)
class ExecutionSettings:
    control: PriorityExecutorSettings
    source_data: PriorityExecutorSettings
    candidate_quotes: ExecutorSettings
    market_quotes: ExecutorSettings
    intraday_minutes: ExecutorSettings
    http_workers: int

    def __post_init__(self) -> None:
        if self.control.urgent_workers < 1:
            raise ValueError("control executor requires an urgent worker")
        if type(self.http_workers) is not int or self.http_workers < 1:
            raise ValueError("HTTP workers must be a positive integer")


@dataclass(frozen=True)
class PipelineSettings:
    event_queue_size: int
    priority_queue_size: int
    executors: ExecutionSettings
    round_timeout_seconds: float
    shutdown_timeout_seconds: float
    long_quote_max_age_seconds: float
    history_preload: HistoryPreloadSettings
    company_preload: CompanyPreloadSettings
    cadence_seconds: Mapping[str, Mapping[str, float]]
    publish_heartbeat_seconds: int


@dataclass(frozen=True)
class MarketDataSettings:
    eastmoney_timeout_seconds: float
    sina_timeout_seconds: float
    full_market_primary_timeout_seconds: float
    full_market_quote_attempts: int
    candidate_timeout_seconds: float
    minimum_market_rows: int
    single_flight: bool
    circuit_breaker_failures: int
    circuit_breaker_seconds: int
    source_contracts: Mapping[str, str]
    tushare: TushareSettings
    cache_policy: CachePolicy


@dataclass(frozen=True)
class TushareSettings:
    enabled: bool
    points: int
    timeout_seconds: float
    token_file: Path | None = None
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
    cache_logical_bytes: int
    process_peak_rss_bytes: int
    growth_percent: float


@dataclass(frozen=True)
class DatabaseMediumPerformanceSettings:
    sqlite_improvement_percent: float
    parquet_improvement_percent: float
    minimum_wins: int
    evidence_status: str


@dataclass(frozen=True)
class DatabasePerformanceSettings:
    hdd: DatabaseMediumPerformanceSettings
    ssd: DatabaseMediumPerformanceSettings

    def for_medium(self, medium: DiskKind) -> DatabaseMediumPerformanceSettings:
        if medium == "hdd":
            return self.hdd
        if medium == "ssd":
            return self.ssd
        raise ValueError("database performance gates require a detected HDD or SSD medium")


@dataclass(frozen=True)
class SqliteSettings:
    hdd: SqliteIoPolicy
    ssd: SqliteIoPolicy

    def for_medium(self, medium: DiskKind) -> SqliteIoPolicy:
        if medium == "hdd":
            return self.hdd
        if medium == "ssd":
            return self.ssd
        raise ValueError("disk policy requires a detected HDD or SSD medium")


@dataclass(frozen=True)
class PerformanceBudgetSettings:
    workload: PerformanceWorkloadSettings
    rounds: PerformanceRoundSettings
    latency_p95_ms: Mapping[str, float]
    data_age_p95_seconds: Mapping[str, float]
    memory: PerformanceMemorySettings
    relative_regression_percent: float
    database: DatabasePerformanceSettings


@dataclass(frozen=True)
class DeepSeekAdaptiveSettings:
    consecutive_failure_limit: int = 2
    rolling_window: int = 5
    minimum_application_ratio: float = 0.4
    healthy_application_ratio: float = 0.6
    healthy_batch_count: int = 3
    cooldown_seconds: int = 900


@dataclass(frozen=True)
class DeepSeekSettings:
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: float
    batch_size: int
    max_tokens: int
    daily_hard_limit: int
    strategy_limits: Mapping[str, int]
    stage_targets: Mapping[str, int]
    stage_limits: Mapping[str, int]
    api_key: str = field(default="", repr=False)
    adaptive: DeepSeekAdaptiveSettings = field(default_factory=DeepSeekAdaptiveSettings)


@dataclass(frozen=True)
class ApiSettings:
    default_top_n: int
    maximum_top_n: int
    sse_history_size: int
    sse_client_queue_size: int
    sse_max_clients: int
    web_snapshot_retention_seconds: float


@dataclass(frozen=True)
class RuntimeSettings:
    config_version: str
    config_path: Path
    project_root: Path
    runtime_dir: Path
    data_paths: DataPaths
    strategy_config_path: Path
    long_watchlist_path: Path
    server: ServerSettings
    pipeline: PipelineSettings
    market_data: MarketDataSettings
    performance_budgets: PerformanceBudgetSettings
    deepseek: DeepSeekSettings
    api: ApiSettings
    sqlite_policy: SqliteSettings
    disk_medium: DiskMedium | None = None

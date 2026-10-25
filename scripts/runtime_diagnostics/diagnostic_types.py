"""Shared immutable value types for the unified runtime diagnostic entrypoint."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId

PROJECT_ROOT = Path(__file__).resolve().parents[2]

Profile = Literal[
    "storage",
    "local",
    "web",
    "history",
    "security-master",
    "tencent",
    "tushare",
    "history-daily-capability",
    "baostock-concurrency",
    "baostock-qfq-shadow",
    "tencent-download",
    "tencent-history-window",
    "v5-training",
    "v5-samples",
    "v5-lite",
    "training-alignment",
    "training-coverage",
    "training-performance",
    "model-inputs",
    "history-sqlite",
    "history-source-compare",
    "market-factor-compare",
    "research-source-compare",
    "history-write",
    "download-performance",
    "company-resume",
    "history-space",
    "history-industry",
    "qfq-sqlite",
    "research",
    "long-watchlist",
    "browser",
    "performance",
    "runtime",
    "sources",
    "live",
    "full",
]
CheckStatus = Literal["passed", "degraded", "failed"]
HistorySource = Literal["composite", "tencent", "eastmoney"]
TencentHistoryHost = Literal["proxy", "direct"]
ScoringProfileSelection = Literal["v1", "v2", "v3", "v5", "both", "v1,v2,v3,v5"]


@dataclass(frozen=True)
class DiagnosticOptions:
    profile: Profile
    base_url: str
    runtime_config: Path
    codes: tuple[str, ...]
    web_samples: int
    web_interval_seconds: float
    source_samples: int
    source_interval_seconds: float
    history_workers: int
    history_days: int
    history_source: HistorySource
    tencent_history_host: TencentHistoryHost
    web_timeout_seconds: float
    source_timeout_seconds: float
    browser_duration_seconds: float
    browser_minimum_updates: int
    command_timeout_seconds: float
    history_root: Path | None
    sqlite_page_sample_count: int
    sqlite_query_rounds: int
    sqlite_revision_write_sample_count: int
    history_resume_samples: int = 0
    baostock_serial_only: bool = False
    baostock_intervals: tuple[float, ...] = (2.0,)
    baostock_sizes: tuple[int, ...] = (10, 50, 100)
    baostock_rounds: int = 1
    long_watchlist: Path = PROJECT_ROOT / "config/long_watchlist.json"
    long_evidence_output: Path | None = None
    company_evidence_database: Path | None = None
    company_replay_directory: Path | None = None
    company_query_audit: bool = False
    v5_probe_directory: Path | None = None
    v5_kernel_probe: bool = False
    training_probe_directory: Path | None = None
    eligibility_evidence: tuple[Path, ...] = ()
    download_sizes: tuple[int, ...] = (10, 50, 100)
    qfq_database: Path | None = None
    scoring_profile: ScoringProfileSelection | None = None
    market_sources: tuple[str, ...] = ("tencent", "eastmoney", "sina", "baostock")
    market_profiles: tuple[str, ...] = ("v2", "v3")
    market_sizes: tuple[int, ...] = (120, 720, 0)
    market_mode: str = "all"
    market_batch_seconds: float = 30.0
    market_source_loop_seconds: float = 120.0
    market_task_seconds: float = 600.0
    output_format: str = "json"
    cpu_profile_dir: Path | None = None
    cpu_profile_mode: str = "functions"
    baseline_checkout: Path | None = None
    training_signal_date: date | None = None
    training_profiles: tuple[ScoringProfileId, ...] = SCORING_PROFILE_IDS
    storage_paths: tuple[Path, ...] = ()
    wide_sample_source: Path | None = None
    v5_sample_root: Path | None = None
    performance_replay_ticks: int = 0
    performance_replay_rows: int | None = None


@dataclass(frozen=True)
class DiagnosticCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    return_code: int
    duration_ms: float
    payload: Mapping[str, object] | None
    error_code: str | None


Runner = Callable[[DiagnosticCommand], DiagnosticResult]

__all__ = [
    "CheckStatus",
    "DiagnosticCommand",
    "DiagnosticOptions",
    "DiagnosticResult",
    "HistorySource",
    "Profile",
    "Runner",
    "ScoringProfileSelection",
    "TencentHistoryHost",
]

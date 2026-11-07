"""Immutable inputs owned by the download/history diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from scripts.core.paths import PROJECT_ROOT
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True)
class HistoryOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    codes: tuple[str, ...] = ('600519', '300750', '688981')
    source_samples: int = 1
    history_workers: int = 5
    history_days: int = 61
    history_source: Literal["composite", "tencent", "eastmoney"] = 'composite'
    tencent_history_host: Literal["proxy", "direct"] = 'proxy'
    source_timeout_seconds: float = 4.5
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    baostock_serial_only: bool = False
    baostock_intervals: tuple[float, ...] = (2.0,)
    baostock_sizes: tuple[int, ...] = (10, 50, 100)
    baostock_rounds: int = 1
    baostock_sessions: int = 0
    baostock_production: bool = False
    history_root: Path | None = None
    download_sizes: tuple[int, ...] = (10, 50, 100)
    database_probe_directory: Path | None = None
    sqlite_page_sample_count: int = 1
    sqlite_query_rounds: int = 3
    sqlite_revision_write_sample_count: int = 512
    history_resume_samples: int = 0
    history_preload_audit: bool = False
    history_preload_cpu_profile: bool = False
    history_preload_feature_audit: bool = False
    history_preload_profiles: tuple[ScoringProfileId, ...] = ('v1', 'v2', 'v3')
    history_preload_context_candidate: str = 'baseline'
    baseline_checkout: Path | None = None
    history_publication_profile: bool = False
    qfq_database: Path | None = None
    capability_universe_size: int = 5453
    capability_code: str = "600519"
    capability_days: int = 120
    capability_timeout_seconds: float = 8.0
    baostock_interval_seconds: float = 2.0
    history_comparison_rounds: int = 5
    history_flush_codes: int | None = None
    history_flush_cells: int | None = None
    official_announcements: tuple[str, ...] = ()
    industry_work_root: Path | None = None
    cninfo_query_codes: tuple[str, ...] = ()
    cninfo_query_from: date | None = None
    cninfo_query_to: date | None = None
    cninfo_cache_evidence: Path | None = None
    cninfo_cache_codes: tuple[str, ...] = ()
    cninfo_cache_candidate: Literal["baseline", "batched", "compare"] = "baseline"

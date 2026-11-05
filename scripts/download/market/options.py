"""Immutable inputs owned by the download/market diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class MarketOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    source_timeout_seconds: float = 4.5
    codes: tuple[str, ...] = ('600519', '300750', '688981')
    source_samples: int = 1
    source_interval_seconds: float = 1.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    history_days: int = 61
    qfq_database: Path | None = None
    market_sources: tuple[str, ...] = ('tencent', 'eastmoney', 'sina', 'baostock')
    market_profiles: tuple[str, ...] = ('v2', 'v3')
    market_sizes: tuple[int, ...] = (120, 720, 0)
    market_mode: str = 'all'
    market_batch_seconds: float = 30.0
    market_source_loop_seconds: float = 120.0
    market_task_seconds: float = 600.0
    cpu_profile_dir: Path | None = None
    security_master_minimum_rows: int = 4000

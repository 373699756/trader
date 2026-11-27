"""Validated configuration values and their public CLI projection."""

from dataclasses import dataclass

from trader.download.domain.download_failure import DownloadFailureConfiguration
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.infra.settings import RuntimeSettings
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.infra.settings.models import StrategySettings
from trader.training.infra.tcn.configuration import TcnConfigurationDocument
from trader.training.infra.tcn.settings import TcnFittingParameters


@dataclass(frozen=True, slots=True)
class ValidatedConfiguration:
    runtime: RuntimeSettings
    strategy: StrategySettings
    watchlist_version: str
    download: HistorySyncConfiguration
    download_failure: DownloadFailureConfiguration
    fit_dates: tuple[tuple[str, int], ...]
    tcn: TcnFittingParameters
    research_arm_count: int
    terminal_holdout_sessions: int
    round_trip_base_rate: float
    evaluation_round_trip_bps: tuple[int, ...]


def ProjectValidatedConfiguration(report: ValidatedConfiguration) -> dict[str, object]:
    runtime, strategy, download = report.runtime, report.strategy, report.download
    fit_dates = dict(report.fit_dates)
    return {
        "status": "ok",
        "runtime_version": runtime.config_version,
        "strategy_version": strategy.strategy_version,
        "scoring_profile": strategy.scoring_profile,
        "prediction_targets": {
            profile: {
                "tomorrow_bps": strategy.prediction_targets.for_profile(profile, Strategy.TOMORROW).threshold_bps,
                "d25_bps": strategy.prediction_targets.for_profile(profile, Strategy.D25).threshold_bps,
            }
            for profile in SCORING_PROFILE_IDS
        },
        "watchlist_version": report.watchlist_version,
        "runtime_dir": str(runtime.runtime_dir),
        "data_dir": str(runtime.data_paths.root),
        "shutdown_timeout_seconds": runtime.pipeline.shutdown_timeout_seconds,
        "history_sessions": download.sessions,
        "history_write_batches": {
            "maximum_codes": download.write_batches.maximum_codes,
            "maximum_cells": download.write_batches.maximum_cells,
        },
        "history_tencent_timeout_seconds": download.tencent_timeout_seconds,
        "history_tencent_retries": download.tencent_retries,
        "download_failure_diagnostics": {"retention_limit": report.download_failure.retention_limit, "query_limit": report.download_failure.query_limit},
        "sqlite_policy": {
            "hdd": {
                "connection_timeout_seconds": runtime.sqlite_policy.hdd.connection_timeout_seconds,
                "read_window_rows": runtime.sqlite_policy.hdd.read_window_rows,
                "transaction_batch_rows": runtime.sqlite_policy.hdd.transaction_batch_rows,
                "checkpoint_mode": runtime.sqlite_policy.hdd.checkpoint_mode,
            },
            "ssd": {
                "connection_timeout_seconds": runtime.sqlite_policy.ssd.connection_timeout_seconds,
                "read_window_rows": runtime.sqlite_policy.ssd.read_window_rows,
                "transaction_batch_rows": runtime.sqlite_policy.ssd.transaction_batch_rows,
                "checkpoint_mode": runtime.sqlite_policy.ssd.checkpoint_mode,
            },
        },
        "training_fit_dates": fit_dates,
        "v5_training_configuration": TcnConfigurationDocument(fit_dates["v5"], report.tcn),
        "research_arms": report.research_arm_count,
        "terminal_holdout_sessions": report.terminal_holdout_sessions,
        "online_round_trip_base_rate": report.round_trip_base_rate,
        "evaluation_round_trip_bps": list(report.evaluation_round_trip_bps),
    }

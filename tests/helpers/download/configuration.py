"""Typed download fixture settings loaded from the current configuration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from tests.helpers.recommendation.configuration import StrategySettingsFixture
from tests.helpers.shared.configuration import PROJECT_ROOT
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import CompanyEvidenceRequest, CompanyListing, CompanySyncConfiguration
from trader.download.domain.download_failure import DownloadFailureConfiguration
from trader.download.domain.history_sync import HistorySyncConfiguration, HistoryWriteBatchPolicy
from trader.download.domain.price_review import PriceReviewConfiguration
from trader.download.infra.download_settings import (
    LoadCompanySyncConfiguration, LoadDownloadFailureConfiguration, LoadHistorySyncConfiguration,
)
from trader.infra.settings.data_paths import DataPaths


class _FailureChanges(TypedDict, total=False):
    retention_limit: int
    query_limit: int


class _EventChanges(TypedDict, total=False):
    share_thresholds: tuple[float, float, float]
    contract_thresholds: tuple[float, float, float]
    risk_days: int
    reduction_tail_days: int
    unlock_days: int


class _HistoryChanges(TypedDict, total=False):
    sessions: int
    reread_sessions: int
    download_batch_size: int
    history_workers: int
    minimum_free_bytes: int
    supplier_timeout_seconds: float
    supplier_retries: int
    query_interval_seconds: float
    cancellation_grace_seconds: float
    progress_heartbeat_seconds: float
    write_batches: HistoryWriteBatchPolicy
    price_review: PriceReviewConfiguration


@lru_cache(maxsize=1)
def CompanySyncConfigurationFixture() -> CompanySyncConfiguration:
    return LoadCompanySyncConfiguration(PROJECT_ROOT)


@lru_cache(maxsize=1)
def _failure_settings() -> DownloadFailureConfiguration:
    return LoadDownloadFailureConfiguration(PROJECT_ROOT)


def DownloadFailureConfigurationFixture(
    *, configuration: DownloadFailureConfiguration | None = None, **changes: Unpack[_FailureChanges],
) -> DownloadFailureConfiguration:
    return replace(configuration if configuration is not None else _failure_settings(), **changes)


def CompanyEventPolicyFixture(
    *, policy: CompanyEventPolicy | None = None, **changes: Unpack[_EventChanges],
) -> CompanyEventPolicy:
    return replace(policy if policy is not None else StrategySettingsFixture().company_scoring.events, **changes)


def HistorySyncConfigurationFixture(
    history_root: Path,
    reference_root: Path,
    *,
    configuration: HistorySyncConfiguration | None = None,
    **changes: Unpack[_HistoryChanges],
) -> HistorySyncConfiguration:
    effective = configuration if configuration is not None else LoadHistorySyncConfiguration(
        PROJECT_ROOT, DataPaths(history_root.parent / "fixture-data"),
    )
    return replace(effective, history_root=history_root, reference_root=reference_root, **changes)


def CompanyEvidenceRequestFixture(
    codes: tuple[str, ...],
    observed_at: datetime,
    configuration: CompanySyncConfiguration,
    event_policy: CompanyEventPolicy | None = None,
    *,
    news_codes: tuple[str, ...] = (),
    stock_names: tuple[tuple[str, str], ...] = (),
    listings: tuple[CompanyListing, ...] = (),
    fill_missing: bool = False,
) -> CompanyEvidenceRequest:
    return CompanyEvidenceRequest(
        codes, observed_at, configuration,
        event_policy if event_policy is not None else CompanyEventPolicyFixture(),
        news_codes=news_codes, stock_names=stock_names, listings=listings, fill_missing=fill_missing,
    )

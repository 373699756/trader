"""Configuration-backed test fixtures without production constructor defaults."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.domain.company_evidence import CompanyEvidenceRequest
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.download.infra.tushare import TushareClient
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings, StrategySettings
from trader.infra.settings.data_paths import DataPaths
from trader.recommendation.domain.candidate.filters import HardFilterPolicy
from trader.recommendation.domain.scoring.company import CompanyScoringPolicy
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_settings import LoadProfileTrainingSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def StrategySettingsFixture() -> StrategySettings:
    return LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json")


def CompanyEventPolicyFixture(**changes: object) -> CompanyEventPolicy:
    return replace(StrategySettingsFixture().company_scoring.events, **changes)


def CompanyScoringPolicyFixture(**changes: object) -> CompanyScoringPolicy:
    return replace(StrategySettingsFixture().company_scoring, **changes)


def HardFilterPolicyFixture(**changes: object) -> HardFilterPolicy:
    settings = StrategySettingsFixture().hard_filters
    policy = HardFilterPolicy(frozenset(settings.blacklist_codes), settings.structured_risk_thresholds)
    return replace(policy, **changes)


def HistorySyncConfigurationFixture(
    history_root: Path,
    reference_root: Path,
    **changes: object,
) -> HistorySyncConfiguration:
    loaded = LoadHistorySyncConfiguration(PROJECT_ROOT, DataPaths(history_root.parent / "fixture-data"))
    return replace(loaded, history_root=history_root, reference_root=reference_root, **changes)


def V1TrainingSettingsFixture(**changes: object) -> V1TrainingSettings:
    return replace(LoadV1TrainingSettings(PROJECT_ROOT), **changes)


def TcnFittingParametersFixture(**changes: object) -> TcnFittingParameters:
    settings = LoadProfileTrainingSettings(PROJECT_ROOT, "v5").fitting
    assert isinstance(settings, TcnFittingParameters)
    return replace(settings, **changes)


def CompanyEvidenceRequestFixture(
    codes: tuple[str, ...],
    observed_at: object,
    configuration: object,
    event_policy: CompanyEventPolicy | None = None,
    **options: object,
) -> CompanyEvidenceRequest:
    return CompanyEvidenceRequest(
        codes=codes,
        observed_at=observed_at,
        configuration=configuration,
        event_policy=event_policy or CompanyEventPolicyFixture(),
        **options,
    )


class SQLiteCompanyEvidenceIndexFixture(SQLiteCompanyEvidenceIndex):
    def __init__(self, *args: object, **options: object) -> None:
        options.setdefault("event_policy", CompanyEventPolicyFixture())
        super().__init__(*args, **options)


class SQLiteCompanyFactsIndexFixture(SQLiteCompanyFactsIndex):
    def __init__(self, *args: object, **options: object) -> None:
        options.setdefault("event_policy", CompanyEventPolicyFixture())
        super().__init__(*args, **options)


class SQLiteCompanyEvidenceReaderFixture(SQLiteCompanyEvidenceReader):
    def __init__(self, *args: object, **options: object) -> None:
        options.setdefault("event_policy", CompanyEventPolicyFixture())
        super().__init__(*args, **options)


class PublishedCompanyResearchFixture(PublishedCompanyResearch):
    def __init__(self, *args: object, **options: object) -> None:
        options.setdefault("company_policy", CompanyScoringPolicyFixture())
        super().__init__(*args, **options)


class CninfoMarketDisclosureSourceFixture(CninfoMarketDisclosureSource):
    def __init__(self, *args: object, **options: object) -> None:
        settings = LoadCompanySyncConfiguration(PROJECT_ROOT)
        options.setdefault("timeout_seconds", settings.source_timeout_seconds)
        options.setdefault("document_parse_timeout_seconds", settings.document_parse_timeout_seconds)
        options.setdefault("request_interval_seconds", settings.request_interval_seconds)
        options.setdefault("document_limits", settings.document_limits)
        options.setdefault("extended_document_limits", settings.extended_document_limits)
        super().__init__(*args, **options)


class EastmoneyCompanySourceFixture(EastmoneyCompanySource):
    def __init__(self, *args: object, **options: object) -> None:
        options.setdefault("timeout_seconds", LoadCompanySyncConfiguration(PROJECT_ROOT).source_timeout_seconds)
        super().__init__(*args, **options)


class CompanyDocumentFilesFixture(CompanyDocumentFiles):
    def __init__(self, root: Path, **options: object) -> None:
        settings = LoadCompanySyncConfiguration(PROJECT_ROOT)
        options.setdefault("max_text_characters", settings.extended_document_limits.max_text_characters)
        options.setdefault("max_bytes", settings.temporary_text_capacity_bytes)
        options.setdefault("reserve_bytes", settings.temporary_disk_reserve_bytes)
        super().__init__(root, **options)


class TushareClientFixture(TushareClient):
    def __init__(self, **options: object) -> None:
        settings = LoadRuntimeSettings(PROJECT_ROOT / "config/runtime.json")
        options.setdefault("points", 2_000)
        options.setdefault("circuit_breaker_failures", settings.market_data.circuit_breaker_failures)
        options.setdefault("circuit_breaker_seconds", settings.market_data.circuit_breaker_seconds)
        super().__init__(**options)


__all__ = [
    "CompanyEventPolicyFixture",
    "CompanyEvidenceRequestFixture",
    "CompanyDocumentFilesFixture",
    "CompanyScoringPolicyFixture",
    "CninfoMarketDisclosureSourceFixture",
    "EastmoneyCompanySourceFixture",
    "HardFilterPolicyFixture",
    "HistorySyncConfigurationFixture",
    "PublishedCompanyResearchFixture",
    "SQLiteCompanyEvidenceIndexFixture",
    "SQLiteCompanyEvidenceReaderFixture",
    "SQLiteCompanyFactsIndexFixture",
    "StrategySettingsFixture",
    "TcnFittingParametersFixture",
    "TushareClientFixture",
    "V1TrainingSettingsFixture",
]

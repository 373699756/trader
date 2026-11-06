"""Explicit inputs for the recommendation company-publication fixture."""

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import TypedDict

from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture
from tests.helpers.shared.configuration import RuntimeSettingsFixture
from trader.infra.settings.models import CompanyPreloadSettings
from trader.recommendation.domain.scoring.company import CompanyScoringPolicy
from trader.recommendation.infra.market_data.published_company_research import (
    CompanyEvidenceReadPort, PublishedCompanyResearch,
)


class _ResearchOptions(TypedDict, total=False):
    reserve_bytes: Callable[[int], bool]
    source_versions: Callable[[], Mapping[str, str]]


class PublishedCompanyResearchFixture(PublishedCompanyResearch):
    def __init__(
        self,
        reader: CompanyEvidenceReadPort,
        *,
        wall_clock: Callable[[], datetime],
        open_dates: Callable[[], tuple[date, ...]],
        monotonic: Callable[[], float],
        company_policy: CompanyScoringPolicy | None = None,
        preload_settings: CompanyPreloadSettings | None = None,
        reserve_bytes: Callable[[int], bool] | None = None,
        source_versions: Callable[[], Mapping[str, str]] | None = None,
        config_version: str = "company",
    ) -> None:
        options: _ResearchOptions = {}
        if reserve_bytes is not None:
            options["reserve_bytes"] = reserve_bytes
        if source_versions is not None:
            options["source_versions"] = source_versions
        super().__init__(
            reader,
            wall_clock=wall_clock,
            open_dates=open_dates,
            monotonic=monotonic,
            company_policy=company_policy if company_policy is not None else CompanyScoringPolicyFixture(),
            preload_settings=preload_settings if preload_settings is not None else RuntimeSettingsFixture().pipeline.company_preload,
            config_version=config_version,
            **options,
        )

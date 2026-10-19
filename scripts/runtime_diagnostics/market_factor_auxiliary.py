"""Bounded, read-only probes for inputs unavailable from spot quote endpoints."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from trader.bootstrap import OpenExternalHttp
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceReader
from trader.infra.http_contracts import HttpError
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.research import LongResearchPolicy, ResearchObservation
from trader.recommendation.domain.market.tail import MinuteBar
from trader.recommendation.infra.market_data.providers.eastmoney import EastmoneyClient
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch


@dataclass(frozen=True)
class AuxiliaryInputs:
    research: Mapping[str, ResearchObservation]
    minutes: Mapping[str, tuple[MinuteBar, ...]]
    report: tuple[dict[str, object], ...]


def ProbeAuxiliary(
    codes: tuple[str, ...],
    at: datetime,
    policy: LongResearchPolicy,
    timeout: float,
    pool: BoundedExecutor,
    *,
    company_database: Path,
) -> AuxiliaryInputs:
    """Read the published risk snapshot; only minute probes contact a supplier."""
    research: dict[str, ResearchObservation] = {}
    minutes: dict[str, tuple[MinuteBar, ...]] = {}
    reports: list[dict[str, object]] = []
    for index, code in enumerate(codes[:3]):
        started = time.monotonic()
        reader = PublishedCompanyResearch(
            SQLiteCompanyEvidenceReader(company_database),
            wall_clock=lambda: at,
        )
        observation = reader.load((code,), at, include_structured=True).get(code)
        if observation is not None:
            research[code] = observation
        reports.append(
            {
                "sample": index + 1,
                "source": "published_company_evidence",
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "physical_http_requests": 0,
                "financial": observation is not None and observation.financial is not None,
                "financial_history_complete": observation is not None and observation.financial_history_complete,
                "announcements": observation is not None and observation.announcements_available,
                "corporate_risk_window_complete": observation is not None
                and observation.corporate_risk_window_complete,
                "pledge": observation is not None and observation.pledge_ratio_pct is not None,
                "unlock": observation is not None and observation.unlock_ratio_pct is not None,
                "component_error_count": len(observation.source_errors) if observation is not None else 1,
            }
        )
        started = time.monotonic()
        error = None
        try:
            bars = EastmoneyClient(
                timeout_seconds=timeout, session_factory=OpenExternalHttp, worker_pool=pool
            ).fetch_intraday_minutes(code, now=at)
        except (OSError, RuntimeError, ValueError, HttpError) as exc:
            bars = ()
            error = type(exc).__name__
        minutes[code] = bars
        reports.append(
            {
                "sample": index + 1,
                "source": "eastmoney_minutes",
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "rows": len(bars),
                "error": error,
            }
        )
    return AuxiliaryInputs(MappingProxyType(research), MappingProxyType(minutes), tuple(reports))


__all__ = ["AuxiliaryInputs", "ProbeAuxiliary"]

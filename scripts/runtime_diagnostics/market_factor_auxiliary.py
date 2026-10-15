"""Bounded, read-only probes for inputs unavailable from spot quote endpoints."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

import requests

from trader.infra.market_data.providers.akshare_http_contracts import AkshareGetFunction, AkshareHttpResponse
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.research import LongResearchPolicy, ResearchObservation
from trader.recommendation.domain.market.tail import MinuteBar
from trader.recommendation.infra.market_data.providers.akshare import AkshareResearchClient
from trader.recommendation.infra.market_data.providers.eastmoney import EastmoneyClient


@dataclass(frozen=True)
class AuxiliaryInputs:
    research: Mapping[str, ResearchObservation]
    minutes: Mapping[str, tuple[MinuteBar, ...]]
    report: tuple[dict[str, object], ...]


def probe_auxiliary(
    codes: tuple[str, ...], at: datetime, policy: LongResearchPolicy, timeout: float, pool: BoundedExecutor
) -> AuxiliaryInputs:
    """Three board samples, at most 12 research requests per code; no cache writes."""
    research: dict[str, ResearchObservation] = {}
    minutes: dict[str, tuple[MinuteBar, ...]] = {}
    reports: list[dict[str, object]] = []
    for index, code in enumerate(codes[:3]):
        started = time.monotonic()
        deadline = started + 20.0
        requests_made = 0

        def bounded_get(url: str, **kwargs: object) -> AkshareHttpResponse:
            nonlocal requests_made
            remaining = deadline - time.monotonic()
            if remaining <= 0 or requests_made >= 12:
                raise requests.Timeout("diagnostic_auxiliary_budget")
            requests_made += 1
            kwargs["timeout"] = min(timeout, remaining)
            transport: AkshareGetFunction = requests.get
            return transport(url, **kwargs)

        client = AkshareResearchClient(
            get=bounded_get,
            timeout_seconds=timeout,
            long_research_policy=policy,
            cancel_requested=lambda: time.monotonic() >= deadline,
        )
        observation = client.fetch_snapshot(code, observed_at=at)
        research[code] = observation
        reports.append(
            {
                "sample": index + 1,
                "source": "eastmoney_research",
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "physical_http_requests": requests_made,
                "financial": observation.financial is not None,
                "financial_history_complete": observation.financial_history_complete,
                "announcements": observation.announcements_available,
                "corporate_risk_history_complete": observation.corporate_risk_history_complete,
                "pledge": observation.pledge_ratio_pct is not None,
                "unlock": observation.unlock_ratio_pct is not None,
                "component_error_count": len(observation.source_errors),
            }
        )
        started = time.monotonic()
        error = None
        try:
            bars = EastmoneyClient(
                timeout_seconds=timeout, session_factory=requests.Session, worker_pool=pool
            ).fetch_intraday_minutes(code, now=at)
        except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
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


__all__ = ["AuxiliaryInputs", "probe_auxiliary"]

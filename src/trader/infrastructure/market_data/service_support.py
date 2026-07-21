"""Pure cache versioning and research serialization helpers."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import ParamSpec, TypeVar

from trader.application.cache import canonical_json_bytes
from trader.domain.models import Evidence, MarketQuote
from trader.domain.research import FinancialReport, ResearchAnnouncement, ResearchObservation
from trader.domain.tail import MinuteBar
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.merge_quote import source_name, source_priority
from trader.infrastructure.market_data.service_models import _ResearchEntry

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _source_batch_identity(
    dataset: str,
    subjects: Sequence[str],
    observed_at: datetime,
    **options: object,
) -> str:
    payload = {
        "dataset": dataset,
        "subjects": sorted(set(subjects)),
        "observed_at": observed_at,
        "options": options,
    }
    return f"{dataset}:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _history_preload_codes(quotes: Sequence[MarketQuote], limit: int) -> tuple[str, ...]:
    groups: dict[str, list[MarketQuote]] = {}
    for quote in quotes:
        if quote.is_suspended or quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
            continue
        groups.setdefault(quote.industry or "unknown", []).append(quote)
    for group in groups.values():
        group.sort(key=_history_priority)
    representatives = sorted((group[0] for group in groups.values()), key=_history_priority)
    selected = representatives[:limit]
    selected_codes = {quote.code for quote in selected}
    remaining = sorted(
        (quote for group in groups.values() for quote in group if quote.code not in selected_codes),
        key=_history_priority,
    )
    selected.extend(remaining[: max(0, limit - len(selected))])
    return tuple(quote.code for quote in selected)


def _normalize_codes(codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))


def _add_action_restriction(
    restrictions: dict[str, set[str]] | None,
    code: str,
    reason: str,
) -> None:
    if restrictions is not None:
        restrictions.setdefault(code, set()).add(reason)


def _quote_version(quote: MarketQuote) -> tuple[datetime, datetime, int, str, str]:
    source = source_name(quote.source)
    return (
        quote.source_time,
        quote.received_time,
        source_priority(source),
        source,
        quote.data_version,
    )


def _quote_age_summary(quotes: Sequence[MarketQuote], measured_at: datetime) -> Mapping[str, object]:
    if not quotes:
        return {
            "sample_count": 0,
            "p50_seconds": None,
            "p95_seconds": None,
            "maximum_seconds": None,
            "latest_source_time": None,
        }
    ages = sorted(quote.age_seconds(measured_at) for quote in quotes)
    return {
        "sample_count": len(ages),
        "p50_seconds": round(ages[max(0, math.ceil(len(ages) * 0.50) - 1)], 3),
        "p95_seconds": round(ages[max(0, math.ceil(len(ages) * 0.95) - 1)], 3),
        "maximum_seconds": round(ages[-1], 3),
        "latest_source_time": max(quote.source_time for quote in quotes).isoformat(),
    }


def _minute_version(bars: Sequence[MinuteBar]) -> tuple[float, float, str]:
    return max(
        ((bar.source_time.timestamp(), bar.received_time.timestamp(), bar.data_version) for bar in bars),
        default=(float("-inf"), float("-inf"), ""),
    )


def _history_version(bars: Sequence[DailyBar]) -> str:
    return max((bar.trade_date for bar in bars), default="")


def _research_version(observation: ResearchObservation) -> tuple[float, float] | None:
    published = [item.published_at.timestamp() for item in observation.announcements]
    published.extend(item.published_at.timestamp() for item in observation.evidence)
    received = [item.received_at.timestamp() for item in observation.evidence if item.received_at is not None]
    if observation.financial is not None:
        published.append(observation.financial.published_at.timestamp())
    if not published:
        return None
    return (max(published), max(received, default=float("-inf")))


def _research_source_time(observation: ResearchObservation) -> datetime | None:
    times = [item.published_at for item in observation.evidence]
    times.extend(item.published_at for item in observation.announcements)
    if observation.financial is not None:
        times.append(observation.financial.published_at)
    return max(times, default=None)


def _research_data_version(observation: ResearchObservation) -> str:
    digest = hashlib.sha256(canonical_json_bytes(observation)).hexdigest()[:20]
    return f"akshare-research:{digest}"


def _research_is_older(observation: ResearchObservation, old_entry: _ResearchEntry | None) -> bool:
    if old_entry is None:
        return False
    current_version = _research_version(observation)
    previous_version = _research_version(old_entry.observation)
    return current_version is not None and previous_version is not None and current_version < previous_version


def _degraded_research_observation(
    old_entry: _ResearchEntry | None,
    error: str,
) -> ResearchObservation:
    normalized_error = error[:240] or "research_refresh_failed"
    if old_entry is None:
        return ResearchObservation(source_errors=(normalized_error,))
    previous = old_entry.observation
    return replace(
        previous,
        source_errors=tuple(dict.fromkeys((*previous.source_errors, normalized_error))),
    )


def _merge_research_observation(
    old_entry: _ResearchEntry | None,
    current: ResearchObservation,
) -> ResearchObservation:
    if old_entry is None or not current.source_errors:
        return current
    previous = old_entry.observation
    failed_sources = {error.partition(":")[0] for error in current.source_errors}
    evidence = tuple({item.evidence_id: item for item in (*previous.evidence, *current.evidence)}.values())[-60:]
    return replace(
        current,
        financial=(
            previous.financial if "financial" in failed_sources and current.financial is None else current.financial
        ),
        announcements=(
            previous.announcements
            if "announcements" in failed_sources and not current.announcements_available
            else current.announcements
        ),
        announcements_available=(
            previous.announcements_available
            if "announcements" in failed_sources and not current.announcements_available
            else current.announcements_available
        ),
        pledge_ratio_pct=(
            previous.pledge_ratio_pct
            if "pledge" in failed_sources and current.pledge_ratio_pct is None
            else current.pledge_ratio_pct
        ),
        unlock_ratio_pct=(
            previous.unlock_ratio_pct
            if "unlock" in failed_sources and current.unlock_ratio_pct is None
            else current.unlock_ratio_pct
        ),
        evidence=evidence,
        source_errors=tuple(dict.fromkeys((*previous.source_errors, *current.source_errors))),
    )


def _history_priority(quote: MarketQuote) -> tuple[float, float, str]:
    return (
        -(quote.amount if quote.amount is not None and math.isfinite(quote.amount) else -1.0),
        -(abs(quote.pct_change) if quote.pct_change is not None and math.isfinite(quote.pct_change) else -1.0),
        quote.code,
    )


def _serialize_research_observation(observation: ResearchObservation) -> dict[str, object]:
    return {
        "financial": _serialize_financial_report(observation.financial) if observation.financial is not None else None,
        "announcements": tuple(_serialize_research_announcement(item) for item in observation.announcements),
        "announcements_available": observation.announcements_available,
        "pledge_ratio_pct": observation.pledge_ratio_pct,
        "unlock_ratio_pct": observation.unlock_ratio_pct,
        "evidence": tuple(_serialize_evidence(item) for item in observation.evidence),
        "source_errors": list(observation.source_errors),
    }


def _deserialize_research_observation(raw: Mapping[str, object]) -> ResearchObservation:
    financial_raw = raw.get("financial")
    announcements_raw = raw.get("announcements")
    evidence_raw = raw.get("evidence")
    source_errors = raw.get("source_errors")
    if not isinstance(source_errors, list):
        raise ValueError("source_errors must be a list")
    return ResearchObservation(
        financial=_deserialize_financial_report(financial_raw) if isinstance(financial_raw, dict) else None,
        announcements=tuple(
            _deserialize_research_announcement(item) for item in announcements_raw if isinstance(item, dict)
        )
        if isinstance(announcements_raw, list)
        else (),
        announcements_available=bool(raw.get("announcements_available", False)),
        pledge_ratio_pct=_optional_float(raw.get("pledge_ratio_pct")),
        unlock_ratio_pct=_optional_float(raw.get("unlock_ratio_pct")),
        evidence=tuple(_deserialize_evidence(item) for item in evidence_raw if isinstance(item, dict))
        if isinstance(evidence_raw, list)
        else (),
        source_errors=tuple(str(value) for value in source_errors),
    )


def _serialize_financial_report(report: FinancialReport) -> dict[str, object]:
    return {
        "report_date": report.report_date.isoformat(),
        "published_at": report.published_at.isoformat(),
        "basic_eps": report.basic_eps,
        "book_value_per_share": report.book_value_per_share,
        "revenue_growth_pct": report.revenue_growth_pct,
        "net_profit_growth_pct": report.net_profit_growth_pct,
        "core_profit_growth_pct": report.core_profit_growth_pct,
        "roe_pct": report.roe_pct,
        "parent_net_profit": report.parent_net_profit,
        "core_net_profit": report.core_net_profit,
    }


def _deserialize_financial_report(raw: Mapping[str, object]) -> FinancialReport:
    report_date = _as_aware_datetime(raw, "report_date").date()
    published_at = _as_aware_datetime(raw, "published_at")
    return FinancialReport(
        report_date=report_date,
        published_at=published_at,
        basic_eps=_optional_float(raw.get("basic_eps")),
        book_value_per_share=_optional_float(raw.get("book_value_per_share")),
        revenue_growth_pct=_optional_float(raw.get("revenue_growth_pct")),
        net_profit_growth_pct=_optional_float(raw.get("net_profit_growth_pct")),
        core_profit_growth_pct=_optional_float(raw.get("core_profit_growth_pct")),
        roe_pct=_optional_float(raw.get("roe_pct")),
        parent_net_profit=_optional_float(raw.get("parent_net_profit")),
        core_net_profit=_optional_float(raw.get("core_net_profit")),
    )


def _serialize_research_announcement(item: ResearchAnnouncement) -> dict[str, object]:
    return {
        "title": item.title,
        "published_at": item.published_at.isoformat(),
    }


def _deserialize_research_announcement(raw: Mapping[str, object]) -> ResearchAnnouncement:
    return ResearchAnnouncement(
        title=str(raw.get("title") or ""),
        published_at=_as_aware_datetime(raw, "published_at"),
    )


def _serialize_evidence(item: Evidence) -> dict[str, object]:
    return {
        "evidence_id": item.evidence_id,
        "evidence_type": item.evidence_type,
        "title": item.title,
        "source": item.source,
        "published_at": item.published_at.isoformat(),
        "received_at": item.received_at.isoformat() if item.received_at is not None else None,
        "data_version": item.data_version,
    }


def _deserialize_evidence(raw: Mapping[str, object]) -> Evidence:
    return Evidence(
        evidence_id=str(raw.get("evidence_id") or ""),
        evidence_type=str(raw.get("evidence_type") or ""),
        title=str(raw.get("title") or ""),
        source=str(raw.get("source") or ""),
        published_at=_as_aware_datetime(raw, "published_at"),
        received_at=as_datetime(raw.get("received_at")),
        data_version=str(raw.get("data_version") or ""),
    )


def _as_aware_datetime(raw: Mapping[str, object], key: str) -> datetime:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a timezone-aware ISO-8601 datetime")
    value_datetime = datetime.fromisoformat(value)
    if value_datetime.tzinfo is None or value_datetime.utcoffset() is None:
        raise ValueError(f"{key} must be a timezone-aware datetime")
    return value_datetime


def as_datetime(raw: object) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("received_at must be ISO-8601 string or null")
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

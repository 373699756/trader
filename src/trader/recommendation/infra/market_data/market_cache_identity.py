"""Pure cache versioning and research serialization."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import ParamSpec, TypeVar

from trader.infra.cache_contracts import CacheRequestKey, RequestKey
from trader.infra.market_data.history.history import DailyBar
from trader.recommendation.domain.market.models import (
    MarketQuote,
)
from trader.recommendation.domain.market.research import (
    ResearchObservation,
)
from trader.recommendation.domain.market.tail import MinuteBar
from trader.recommendation.infra.normalization.merge_quote import SourceName, SourcePriority

_P = ParamSpec("_P")
_T = TypeVar("_T")
_HISTORY_PRELOAD_PER_BOARD_LIMIT = 120


def _reference_epoch(versions: Mapping[str, str]) -> str:
    """Return the single immutable identity for the active static reference set."""

    return f"reference:{tuple(sorted(versions.items()))!r}"


def _source_batch_identity(
    dataset: str,
    subjects: Sequence[str],
    observed_at: datetime,
    **options: object,
) -> CacheRequestKey:
    payload = {
        "dataset": dataset,
        "subjects": sorted(set(subjects)),
        "observed_at": observed_at,
        "options": options,
    }
    return RequestKey(payload)


def _history_preload_codes(quotes: Sequence[MarketQuote], limit: int) -> tuple[str, ...]:
    board_groups: dict[str, dict[str, list[MarketQuote]]] = {}
    for quote in quotes:
        if quote.is_suspended or quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
            continue
        board_groups.setdefault(_history_board(quote.code), {}).setdefault(quote.industry or "unknown", []).append(
            quote
        )
    board_rankings: dict[str, list[MarketQuote]] = {}
    for board, groups in board_groups.items():
        for group in groups.values():
            group.sort(key=_history_priority)
        representatives = sorted((group[0] for group in groups.values()), key=_history_priority)
        represented = {quote.code for quote in representatives}
        remaining = sorted(
            (quote for group in groups.values() for quote in group if quote.code not in represented),
            key=_history_priority,
        )
        board_rankings[board] = [*representatives, *remaining][:_HISTORY_PRELOAD_PER_BOARD_LIMIT]

    selected: list[str] = []
    board_order = ("main", "chinext", "star")
    index = 0
    while len(selected) < limit:
        added = False
        for board in board_order:
            ranking = board_rankings.get(board, ())
            if index < len(ranking):
                selected.append(ranking[index].code)
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        index += 1
    return tuple(selected)


def _history_population_codes(quotes: Sequence[MarketQuote]) -> tuple[str, ...]:
    """Return the complete supported population for compact history preparation.

    Candidate ranking limits are intentionally not applied here.  The bounded
    ``_history_preload_codes`` helper remains available for hot raw-bar loads,
    while this index supplies the summaries needed by the full-market filter.
    """
    board_order = {"main": 0, "chinext": 1, "star": 2}
    eligible = {
        quote.code: quote
        for quote in quotes
        if not quote.is_suspended
        and quote.price is not None
        and math.isfinite(quote.price)
        and quote.price > 0
        and _history_board(quote.code) in board_order
    }
    return tuple(
        quote.code
        for quote in sorted(
            eligible.values(),
            key=lambda quote: (
                board_order[_history_board(quote.code)],
                quote.industry or "unknown",
                quote.code,
            ),
        )
    )


def _history_board(code: str) -> str:
    if code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
        return "main"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("688", "689")):
        return "star"
    return "unsupported"


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
    source = SourceName(quote.source)
    return (
        quote.source_time,
        quote.received_time,
        SourcePriority(source),
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
    return observation.data_version


def _history_priority(quote: MarketQuote) -> tuple[float, float, str]:
    return (
        -(quote.amount if quote.amount is not None and math.isfinite(quote.amount) else -1.0),
        -(abs(quote.pct_change) if quote.pct_change is not None and math.isfinite(quote.pct_change) else -1.0),
        quote.code,
    )


def AsDatetime(raw: object) -> datetime | None:
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

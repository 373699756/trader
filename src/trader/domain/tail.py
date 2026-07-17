"""Pure point-in-time calculations for tomorrow tail-session signals."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from trader.domain.factors import clamp
from trader.domain.models import Evidence

SHANGHAI = ZoneInfo("Asia/Shanghai")
TAIL_SIGNAL_VALUE_FIELDS = (
    "tail_return_30m_pct",
    "tail_return_30m",
    "tail_volume_ratio_raw",
    "tail_volume_ratio",
)


@dataclass(frozen=True)
class MinuteBar:
    source_time: datetime
    close: float
    volume: float | None
    source: str
    received_time: datetime
    data_version: str

    def __post_init__(self) -> None:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.source_time, self.received_time)):
            raise ValueError("minute bar times must be timezone-aware")


@dataclass(frozen=True)
class TailSignalPolicy:
    lookback_minutes: int
    minimum_baseline_minutes: int
    return_score_points_per_pct: float
    volume_score_points_per_ratio: float

    def __post_init__(self) -> None:
        if self.lookback_minutes < 1 or self.minimum_baseline_minutes < 1:
            raise ValueError("tail minute windows must be positive")
        if (
            not math.isfinite(self.return_score_points_per_pct)
            or self.return_score_points_per_pct <= 0
            or not math.isfinite(self.volume_score_points_per_ratio)
            or self.volume_score_points_per_ratio <= 0
        ):
            raise ValueError("tail score scales must be finite and positive")


@dataclass(frozen=True)
class TailSignals:
    return_pct: float | None
    return_score: float | None
    volume_ratio: float | None
    volume_score: float | None
    reference_price: float | None
    latest_price: float | None
    baseline_mean_volume: float | None
    tail_mean_volume: float | None
    latest_at: datetime | None
    received_at: datetime | None
    source: str
    data_versions: tuple[str, ...]
    valid_bar_count: int


def derive_tail_signals(
    bars: tuple[MinuteBar, ...],
    *,
    observed_at: datetime,
    policy: TailSignalPolicy,
) -> TailSignals:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("tail observation time must be timezone-aware")
    observed_local = observed_at.astimezone(SHANGHAI)
    unique = _unique_point_in_time_bars(bars, observed_local)
    if not unique:
        return TailSignals(None, None, None, None, None, None, None, None, None, None, "", (), 0)

    ordered = sorted(unique.values(), key=lambda item: item[0])
    contiguous = _latest_contiguous_suffix(ordered)
    latest = contiguous[-1][1]
    reference_price: float | None = None
    return_pct: float | None = None
    return_score: float | None = None
    required_return_bars = policy.lookback_minutes + 1
    if len(contiguous) >= required_return_bars:
        reference_price = contiguous[-required_return_bars][1].close
        raw_return = (latest.close / reference_price - 1.0) * 100.0
        if math.isfinite(raw_return):
            return_pct = raw_return
            return_score = clamp(50.0 + return_pct * policy.return_score_points_per_pct)

    volume_ratio: float | None = None
    volume_score: float | None = None
    baseline_mean: float | None = None
    tail_mean: float | None = None
    if len(contiguous) >= policy.lookback_minutes:
        tail = contiguous[-policy.lookback_minutes :]
        tail_volumes = _valid_volumes(item[1] for item in tail)
        tail_first_ordinal = tail[0][0]
        baseline_volumes = _valid_volumes(item[1] for item in ordered if item[0] < tail_first_ordinal)
        if len(tail_volumes) == policy.lookback_minutes and len(baseline_volumes) >= policy.minimum_baseline_minutes:
            baseline_mean = _finite_mean(baseline_volumes)
            tail_mean = _finite_mean(tail_volumes)
            if baseline_mean is not None and baseline_mean > 0.0 and tail_mean is not None:
                volume_ratio = tail_mean / baseline_mean
                if math.isfinite(volume_ratio):
                    volume_score = clamp(50.0 + (volume_ratio - 1.0) * policy.volume_score_points_per_ratio)
                else:
                    volume_ratio = None

    sources = sorted({item[1].source for item in ordered if item[1].source})
    data_versions = tuple(sorted({item[1].data_version for item in ordered if item[1].data_version}))
    return TailSignals(
        return_pct=return_pct,
        return_score=return_score,
        volume_ratio=volume_ratio,
        volume_score=volume_score,
        reference_price=reference_price,
        latest_price=latest.close,
        baseline_mean_volume=baseline_mean,
        tail_mean_volume=tail_mean,
        latest_at=latest.source_time,
        received_at=max(item[1].received_time for item in ordered),
        source="+".join(sources),
        data_versions=data_versions,
        valid_bar_count=len(ordered),
    )


def tail_signal_evidence(code: str, signals: TailSignals) -> Evidence | None:
    if signals.latest_at is None:
        return None
    title = (
        f"尾盘分钟输入：参考价={_format_number(signals.reference_price)}，"
        f"最新价={_format_number(signals.latest_price)}，"
        f"30分钟收益={_format_number(signals.return_pct)}%，"
        f"尾盘均量={_format_number(signals.tail_mean_volume)}，"
        f"基线均量={_format_number(signals.baseline_mean_volume)}，"
        f"量比={_format_number(signals.volume_ratio)}，"
        f"有效分钟={signals.valid_bar_count}"
    )
    identity = hashlib.sha256(
        repr(
            (
                code,
                signals.latest_at.isoformat(),
                signals.return_pct,
                signals.volume_ratio,
                signals.reference_price,
                signals.latest_price,
                signals.baseline_mean_volume,
                signals.tail_mean_volume,
                signals.source,
                signals.data_versions,
            )
        ).encode("utf-8")
    ).hexdigest()[:32]
    return Evidence(
        evidence_id=f"intraday-tail:{code}:{identity}",
        evidence_type="intraday_tail",
        title=title[:240],
        source=signals.source or "intraday_unavailable",
        published_at=signals.latest_at,
        received_at=signals.received_at,
        data_version="+".join(signals.data_versions),
    )


def _unique_point_in_time_bars(
    bars: tuple[MinuteBar, ...], observed_local: datetime
) -> dict[int, tuple[int, MinuteBar]]:
    grouped: dict[int, list[MinuteBar]] = {}
    for bar in bars:
        local = bar.source_time.astimezone(SHANGHAI)
        received_local = bar.received_time.astimezone(SHANGHAI)
        ordinal = _trading_minute_ordinal(local)
        if (
            ordinal is None
            or local.date() != observed_local.date()
            or bar.source_time > observed_local
            or received_local < local
            or received_local > observed_local
            or not math.isfinite(bar.close)
            or bar.close <= 0.0
        ):
            continue
        grouped.setdefault(ordinal, []).append(bar)
    return {ordinal: (ordinal, items[0]) for ordinal, items in grouped.items() if len(items) == 1}


def _latest_contiguous_suffix(ordered: list[tuple[int, MinuteBar]]) -> list[tuple[int, MinuteBar]]:
    start = len(ordered) - 1
    while start > 0 and ordered[start][0] - ordered[start - 1][0] == 1:
        start -= 1
    return ordered[start:]


def _valid_volumes(bars: Iterable[MinuteBar]) -> list[float]:
    result: list[float] = []
    for bar in bars:
        volume = bar.volume
        if volume is not None and math.isfinite(volume) and volume >= 0.0:
            result.append(volume)
    return result


def _finite_mean(values: list[float]) -> float | None:
    try:
        result = math.fsum(values) / len(values)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _trading_minute_ordinal(value: datetime) -> int | None:
    current = value.time().replace(tzinfo=None)
    if value.second != 0 or value.microsecond != 0:
        return None
    if time(9, 31) <= current <= time(11, 30):
        return (value.hour * 60 + value.minute) - (9 * 60 + 31)
    if time(13, 1) <= current <= time(15, 0):
        return 120 + (value.hour * 60 + value.minute) - (13 * 60 + 1)
    return None


def _format_number(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


__all__ = [
    "MinuteBar",
    "TAIL_SIGNAL_VALUE_FIELDS",
    "TailSignalPolicy",
    "TailSignals",
    "derive_tail_signals",
    "tail_signal_evidence",
]

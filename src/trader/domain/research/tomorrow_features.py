"""Pure point-in-time feature engineering for the offline Tomorrow challenger."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import fmean, pstdev
from typing import Literal

from trader.domain.research.historical import SUPPORTED_RESEARCH_BOARDS, ResearchBoard

TomorrowFeatureFamily = Literal[
    "residual_reversal",
    "residual_momentum",
    "overnight",
    "intraday",
    "tail",
]
PublishedFeatureKind = Literal["financial", "announcement"]

_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_REVERSAL_HORIZONS = (1, 3, 5)
_MOMENTUM_HORIZONS = (20, 40, 60)
TOMORROW_FEATURE_NAMES = (
    "residual_reversal_1d",
    "residual_reversal_3d",
    "residual_reversal_5d",
    "residual_momentum_20_5",
    "residual_momentum_40_5",
    "residual_momentum_60_5",
    "overnight_gap",
    "intraday_return",
    "morning_return",
    "afternoon_return",
    "tail_return_30m",
    "close_location",
    "tail_amount_share",
)


@dataclass(frozen=True)
class DailyFeaturePoint:
    session_date: date
    close: float
    amount: float

    def __post_init__(self) -> None:
        _finite_positive(self.close, "daily close")
        _finite_non_negative(self.amount, "daily amount")


@dataclass(frozen=True)
class IntradayFeaturePoint:
    observed_at: datetime
    close: float
    amount: float

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at, "intraday feature time")
        _finite_positive(self.close, "intraday close")
        _finite_non_negative(self.amount, "intraday amount")


@dataclass(frozen=True)
class PointInTimePublishedFact:
    kind: PublishedFeatureKind
    name: str
    value: float
    report_period: date | None
    published_at: datetime
    received_at: datetime
    source: str
    evidence_hash: str

    def __post_init__(self) -> None:
        if self.kind not in {"financial", "announcement"}:
            raise ValueError("published feature kind is invalid")
        if not self.name.strip() or not self.source.strip() or _SHA256.fullmatch(self.evidence_hash) is None:
            raise ValueError("published feature identity is invalid")
        if not math.isfinite(self.value):
            raise ValueError("published feature value must be finite")
        _require_shanghai(self.published_at, "published feature published_at")
        _require_shanghai(self.received_at, "published feature received_at")
        if self.received_at < self.published_at:
            raise ValueError("published feature cannot be received before publication")
        if self.report_period is not None and self.report_period > self.published_at.date():
            raise ValueError("published feature report period cannot follow publication")


@dataclass(frozen=True)
class TomorrowFeatureStockInput:
    code: str
    board: ResearchBoard
    industry: str
    industry_effective_at: datetime
    industry_received_at: datetime
    as_of: datetime
    daily_points: tuple[DailyFeaturePoint, ...]
    intraday_points: tuple[IntradayFeaturePoint, ...]
    current_open: float
    current_high: float
    current_low: float
    current_last: float
    market_cap: float | None
    liquidity: float | None
    published_facts: tuple[PointInTimePublishedFact, ...] = ()

    def __post_init__(self) -> None:
        _validate_stock_identity(self)
        daily = tuple(sorted(self.daily_points, key=lambda item: item.session_date))
        intraday = tuple(sorted(self.intraday_points, key=lambda item: item.observed_at))
        facts = tuple(
            sorted(
                self.published_facts,
                key=lambda item: (item.kind, item.name, item.published_at, item.source, item.evidence_hash),
            )
        )
        _validate_stock_observations(self, daily, intraday, facts)
        _validate_current_prices(self)
        _optional_positive(self.market_cap, "market cap")
        _optional_positive(self.liquidity, "liquidity")
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "daily_points", daily)
        object.__setattr__(self, "intraday_points", intraday)
        object.__setattr__(self, "published_facts", facts)


def _validate_stock_identity(item: TomorrowFeatureStockInput) -> None:
    _require_code(item.code)
    if item.board not in SUPPORTED_RESEARCH_BOARDS or not item.industry.strip():
        raise ValueError("Tomorrow feature security identity is invalid")
    _require_shanghai(item.as_of, "Tomorrow feature cutoff")
    _require_shanghai(item.industry_effective_at, "industry effective time")
    _require_shanghai(item.industry_received_at, "industry received time")
    if item.industry_effective_at > item.as_of or item.industry_received_at > item.as_of:
        raise ValueError("industry identity is after feature cutoff")
    if item.industry_received_at < item.industry_effective_at:
        raise ValueError("industry identity cannot be received before it is effective")


def _validate_stock_observations(
    item: TomorrowFeatureStockInput,
    daily: tuple[DailyFeaturePoint, ...],
    intraday: tuple[IntradayFeaturePoint, ...],
    facts: tuple[PointInTimePublishedFact, ...],
) -> None:
    if len({point.session_date for point in daily}) != len(daily):
        raise ValueError("daily feature points must be unique")
    if any(point.session_date >= item.as_of.date() for point in daily):
        raise ValueError("daily features must come from completed sessions")
    if len({point.observed_at for point in intraday}) != len(intraday):
        raise ValueError("intraday feature points must be unique")
    if any(point.observed_at.date() != item.as_of.date() or point.observed_at > item.as_of for point in intraday):
        raise ValueError("intraday feature point is after feature cutoff")
    if len({(fact.kind, fact.name, fact.source, fact.evidence_hash) for fact in facts}) != len(facts):
        raise ValueError("published feature facts must be unique")
    if any(fact.published_at > item.as_of for fact in facts):
        raise ValueError("published feature was published after feature cutoff")
    if any(fact.received_at > item.as_of for fact in facts):
        raise ValueError("published feature was received after feature cutoff")


@dataclass(frozen=True)
class TomorrowFeatureValue:
    name: str
    family: TomorrowFeatureFamily
    value: float | None

    def __post_init__(self) -> None:
        if not self.name or self.family not in {
            "residual_reversal",
            "residual_momentum",
            "overnight",
            "intraday",
            "tail",
        }:
            raise ValueError("Tomorrow feature identity is invalid")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("Tomorrow feature value must be finite")


@dataclass(frozen=True)
class TomorrowStockFeatures:
    code: str
    board: ResearchBoard
    industry: str
    industry_effective_at: datetime
    industry_received_at: datetime
    as_of: datetime
    market_cap: float | None
    liquidity: float | None
    values: tuple[TomorrowFeatureValue, ...]
    missing_fields: tuple[str, ...]
    published_facts: tuple[PointInTimePublishedFact, ...]

    def __post_init__(self) -> None:
        _require_code(self.code)
        if self.board not in SUPPORTED_RESEARCH_BOARDS or not self.industry:
            raise ValueError("Tomorrow feature row identity is invalid")
        _require_shanghai(self.as_of, "Tomorrow feature row cutoff")
        _require_shanghai(self.industry_effective_at, "Tomorrow feature row industry effective time")
        _require_shanghai(self.industry_received_at, "Tomorrow feature row industry received time")
        if self.industry_effective_at > self.as_of or self.industry_received_at > self.as_of:
            raise ValueError("Tomorrow feature row industry identity is after cutoff")
        _optional_positive(self.market_cap, "Tomorrow feature row market cap")
        _optional_positive(self.liquidity, "Tomorrow feature row liquidity")
        if len({item.name for item in self.values}) != len(self.values):
            raise ValueError("Tomorrow feature names must be unique")
        expected_missing = tuple(item.name for item in self.values if item.value is None)
        if self.missing_fields != expected_missing:
            raise ValueError("Tomorrow feature missing mask does not match values")


def build_tomorrow_stock_features(
    stocks: tuple[TomorrowFeatureStockInput, ...],
) -> tuple[TomorrowStockFeatures, ...]:
    """Build the fixed five research feature families without production authority."""

    ordered = tuple(sorted(stocks, key=lambda item: item.code))
    if not ordered or len({item.code for item in ordered}) != len(ordered):
        raise ValueError("Tomorrow feature population must be non-empty and unique")
    if len({item.as_of for item in ordered}) != 1:
        raise ValueError("Tomorrow feature population must share one cutoff")

    reversal = {
        horizon: _neutralize(
            {item.code: _current_return(item, horizon) for item in ordered},
            ordered,
        )
        for horizon in _REVERSAL_HORIZONS
    }
    momentum = {
        horizon: _neutralize(
            {item.code: _skip_recent_return(item, horizon) for item in ordered},
            ordered,
        )
        for horizon in _MOMENTUM_HORIZONS
    }

    result: list[TomorrowStockFeatures] = []
    for item in ordered:
        values = _feature_values(item, reversal, momentum)
        result.append(
            TomorrowStockFeatures(
                code=item.code,
                board=item.board,
                industry=item.industry,
                industry_effective_at=item.industry_effective_at,
                industry_received_at=item.industry_received_at,
                as_of=item.as_of,
                market_cap=item.market_cap,
                liquidity=item.liquidity,
                values=values,
                missing_fields=tuple(value.name for value in values if value.value is None),
                published_facts=item.published_facts,
            )
        )
    return tuple(result)


def _feature_values(
    item: TomorrowFeatureStockInput,
    reversal: dict[int, dict[str, float]],
    momentum: dict[int, dict[str, float]],
) -> tuple[TomorrowFeatureValue, ...]:
    values: list[TomorrowFeatureValue] = []
    for horizon in _REVERSAL_HORIZONS:
        residual = reversal[horizon].get(item.code)
        values.append(
            TomorrowFeatureValue(
                f"residual_reversal_{horizon}d",
                "residual_reversal",
                -residual if residual is not None else None,
            )
        )
    for horizon in _MOMENTUM_HORIZONS:
        residual = momentum[horizon].get(item.code)
        volatility = _skip_recent_volatility(item, horizon)
        scaled = residual / volatility if residual is not None and volatility not in {None, 0.0} else None
        values.append(TomorrowFeatureValue(f"residual_momentum_{horizon}_5", "residual_momentum", scaled))
    values.extend(
        (
            TomorrowFeatureValue("overnight_gap", "overnight", _overnight_gap(item)),
            TomorrowFeatureValue("intraday_return", "intraday", _ratio(item.current_last, item.current_open)),
            TomorrowFeatureValue("morning_return", "intraday", _morning_return(item)),
            TomorrowFeatureValue("afternoon_return", "intraday", _afternoon_return(item)),
            TomorrowFeatureValue("tail_return_30m", "tail", _tail_return(item)),
            TomorrowFeatureValue("close_location", "tail", _close_location(item)),
            TomorrowFeatureValue("tail_amount_share", "tail", _tail_amount_share(item)),
        )
    )
    return tuple(values)


def _current_return(item: TomorrowFeatureStockInput, horizon: int) -> float | None:
    if len(item.daily_points) < horizon:
        return None
    return _ratio(item.current_last, item.daily_points[-horizon].close)


def _skip_recent_return(item: TomorrowFeatureStockInput, horizon: int) -> float | None:
    if len(item.daily_points) < horizon or len(item.daily_points) < 5:
        return None
    return _ratio(item.daily_points[-5].close, item.daily_points[-horizon].close)


def _skip_recent_volatility(item: TomorrowFeatureStockInput, horizon: int) -> float | None:
    if len(item.daily_points) < horizon:
        return None
    closes = tuple(point.close for point in item.daily_points[-horizon:-4])
    returns = tuple(_ratio(current, previous) for previous, current in zip(closes, closes[1:], strict=False))
    known = tuple(value for value in returns if value is not None)
    if len(known) < 2:
        return None
    value = pstdev(known)
    return value if value > 0.0 else None


def _neutralize(
    raw: dict[str, float | None],
    stocks: tuple[TomorrowFeatureStockInput, ...],
) -> dict[str, float]:
    by_code = {item.code: item for item in stocks}
    eligible = {
        code: value
        for code, value in raw.items()
        if value is not None and by_code[code].market_cap is not None and by_code[code].liquidity is not None
    }
    if len(eligible) < 3:
        return {}
    residual = _demean(eligible)
    residual = _group_demean(residual, by_code, "board")
    residual = _group_demean(residual, by_code, "industry")
    if len(residual) < 3:
        return {}
    return _remove_continuous_exposure(residual, by_code)


def _demean(values: dict[str, float]) -> dict[str, float]:
    mean = fmean(values.values())
    return {code: value - mean for code, value in values.items()}


def _group_demean(
    values: dict[str, float],
    stocks: dict[str, TomorrowFeatureStockInput],
    field: Literal["board", "industry"],
) -> dict[str, float]:
    groups: defaultdict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, value in values.items():
        groups[str(getattr(stocks[code], field))].append((code, value))
    result: dict[str, float] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        mean = fmean(value for _code, value in group)
        result.update({code: value - mean for code, value in group})
    return result


def _remove_continuous_exposure(
    values: dict[str, float],
    stocks: dict[str, TomorrowFeatureStockInput],
) -> dict[str, float]:
    codes = tuple(sorted(values))
    cap = tuple(math.log1p(stocks[code].market_cap or 0.0) for code in codes)
    liquidity = tuple(math.log1p(stocks[code].liquidity or 0.0) for code in codes)
    cap_mean = fmean(cap)
    liquidity_mean = fmean(liquidity)
    x1 = tuple(value - cap_mean for value in cap)
    x2 = tuple(value - liquidity_mean for value in liquidity)
    y = tuple(values[code] for code in codes)
    ridge = 1e-12
    a = sum(value * value for value in x1) + ridge
    b = sum(left * right for left, right in zip(x1, x2, strict=True))
    d = sum(value * value for value in x2) + ridge
    c1 = sum(left * right for left, right in zip(x1, y, strict=True))
    c2 = sum(left * right for left, right in zip(x2, y, strict=True))
    determinant = a * d - b * b
    beta1 = (c1 * d - c2 * b) / determinant
    beta2 = (c2 * a - c1 * b) / determinant
    return {
        code: value - beta1 * first - beta2 * second
        for code, value, first, second in zip(codes, y, x1, x2, strict=True)
    }


def _overnight_gap(item: TomorrowFeatureStockInput) -> float | None:
    if not item.daily_points:
        return None
    return _ratio(item.current_open, item.daily_points[-1].close)


def _morning_return(item: TomorrowFeatureStockInput) -> float | None:
    morning = tuple(point for point in item.intraday_points if point.observed_at.time() <= _MORNING_END)
    if not morning:
        return None
    return _ratio(morning[-1].close, item.current_open)


def _afternoon_return(item: TomorrowFeatureStockInput) -> float | None:
    afternoon = tuple(point for point in item.intraday_points if point.observed_at.time() >= _AFTERNOON_START)
    if not afternoon:
        return None
    return _ratio(item.current_last, afternoon[0].close)


def _tail_return(item: TomorrowFeatureStockInput) -> float | None:
    cutoff = item.as_of - timedelta(minutes=30)
    anchors = tuple(point for point in item.intraday_points if point.observed_at == cutoff)
    if not anchors:
        return None
    return _ratio(item.current_last, anchors[-1].close)


def _close_location(item: TomorrowFeatureStockInput) -> float | None:
    spread = item.current_high - item.current_low
    if spread <= 0.0:
        return None
    return (item.current_last - item.current_low) / spread


def _tail_amount_share(item: TomorrowFeatureStockInput) -> float | None:
    total = sum(point.amount for point in item.intraday_points)
    if total <= 0.0:
        return None
    cutoff = item.as_of - timedelta(minutes=30)
    tail = sum(point.amount for point in item.intraday_points if point.observed_at >= cutoff)
    return tail / total


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0.0:
        return None
    return numerator / denominator - 1.0


def _validate_current_prices(item: TomorrowFeatureStockInput) -> None:
    for value in (item.current_open, item.current_high, item.current_low, item.current_last):
        _finite_positive(value, "current session price")
    if item.current_low > min(item.current_open, item.current_last):
        raise ValueError("current low is inconsistent")
    if item.current_high < max(item.current_open, item.current_last):
        raise ValueError("current high is inconsistent")


def _require_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("Tomorrow feature code must contain six digits")


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{label} must use Asia/Shanghai")


def _finite_positive(value: float, label: str) -> None:
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{label} must be finite and positive")


def _finite_non_negative(value: float, label: str) -> None:
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")


def _optional_positive(value: float | None, label: str) -> None:
    if value is not None:
        _finite_positive(value, label)


__all__ = [
    "DailyFeaturePoint",
    "IntradayFeaturePoint",
    "PointInTimePublishedFact",
    "PublishedFeatureKind",
    "TomorrowFeatureFamily",
    "TOMORROW_FEATURE_NAMES",
    "TomorrowFeatureStockInput",
    "TomorrowFeatureValue",
    "TomorrowStockFeatures",
    "build_tomorrow_stock_features",
]

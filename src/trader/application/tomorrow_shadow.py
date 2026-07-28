"""Bounded engineering evidence for tomorrow v2 shadow cutover."""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime

_SHANGHAI_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class TomorrowCutoverPolicy:
    minimum_samples: int = 100
    minimum_trade_days: int = 1
    maximum_samples: int = 4096
    local_publish_p95_seconds: float = 5.0
    decision_age_p95_seconds: float = 10.0

    def __post_init__(self) -> None:
        if min(self.minimum_samples, self.minimum_trade_days, self.maximum_samples) < 1:
            raise ValueError("tomorrow cutover sample limits must be positive")
        if self.minimum_samples > self.maximum_samples:
            raise ValueError("tomorrow minimum samples cannot exceed retained samples")
        for value in (self.local_publish_p95_seconds, self.decision_age_p95_seconds):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError("tomorrow cutover latency limits must be finite and positive")


@dataclass(frozen=True)
class TomorrowShadowObservation:
    trade_date: date
    observed_at: datetime
    baseline_snapshot_id: str
    decision_version: str
    input_version: str
    selected_codes_match: bool
    filter_reasons_match: bool
    local_publish_seconds: float
    decision_age_seconds: float
    deepseek_request_delta: int
    resource_limits_passed: bool
    baseline_frozen: bool
    v2_frozen: bool
    freeze_codes_match: bool
    processing_error: str = ""

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at)
        if not all(
            value.strip()
            for value in (
                self.baseline_snapshot_id,
                self.decision_version,
                self.input_version,
            )
        ):
            raise ValueError("tomorrow shadow identities must not be empty")
        for value in (self.local_publish_seconds, self.decision_age_seconds):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("tomorrow shadow latency values must be finite and non-negative")
        if self.deepseek_request_delta < 0:
            raise ValueError("tomorrow shadow DeepSeek request delta cannot be negative")
        if self.v2_frozen and not self.baseline_frozen:
            raise ValueError("tomorrow shadow v2 freeze cannot precede the baseline freeze")
        if self.freeze_codes_match and not (self.baseline_frozen and self.v2_frozen):
            raise ValueError("matching freeze codes require both chains to be frozen")


@dataclass(frozen=True)
class TomorrowCutoverStatus:
    eligible: bool
    blockers: tuple[str, ...]
    sample_count: int
    successful_sample_count: int
    trade_day_count: int
    selection_agreement_ratio: float | None
    filter_agreement_ratio: float | None
    local_publish_p95_seconds: float | None
    decision_age_p95_seconds: float | None
    processing_error_count: int
    deepseek_request_delta: int
    matching_freeze_count: int
    resource_failure_count: int


class TomorrowCutoverGate:
    """Collects bounded shadow evidence without performing a cutover."""

    def __init__(self, policy: TomorrowCutoverPolicy | None = None) -> None:
        self._policy = policy or TomorrowCutoverPolicy()
        self._lock = threading.RLock()
        self._samples: deque[TomorrowShadowObservation] = deque(maxlen=self._policy.maximum_samples)

    def record(self, observation: TomorrowShadowObservation) -> None:
        with self._lock:
            identity = _observation_identity(observation)
            for index, item in enumerate(self._samples):
                if _observation_identity(item) != identity:
                    continue
                if observation.observed_at > item.observed_at:
                    self._samples[index] = observation
                return
            self._samples.append(observation)

    def status(self) -> TomorrowCutoverStatus:
        with self._lock:
            samples = tuple(self._samples)
        return _cutover_status(self._policy, samples)


def _cutover_status(
    policy: TomorrowCutoverPolicy,
    samples: tuple[TomorrowShadowObservation, ...],
) -> TomorrowCutoverStatus:
    sample_count = len(samples)
    successful = tuple(item for item in samples if not item.processing_error)
    successful_sample_count = len(successful)
    trade_day_count = len({item.trade_date for item in successful})
    selection_ratio = _ratio(successful, "selected_codes_match")
    filter_ratio = _ratio(successful, "filter_reasons_match")
    local_p95 = _nearest_rank_p95(tuple(item.local_publish_seconds for item in successful))
    age_p95 = _nearest_rank_p95(tuple(item.decision_age_seconds for item in successful))
    processing_errors = sum(bool(item.processing_error) for item in samples)
    deepseek_delta = sum(item.deepseek_request_delta for item in samples)
    matching_freezes = sum(item.baseline_frozen and item.v2_frozen and item.freeze_codes_match for item in successful)
    resource_failures = sum(not item.resource_limits_passed for item in samples)
    blocker_conditions = (
        (successful_sample_count < policy.minimum_samples, "insufficient_samples"),
        (trade_day_count < policy.minimum_trade_days, "insufficient_trade_days"),
        (
            age_p95 is not None and age_p95 > policy.decision_age_p95_seconds,
            "decision_age_p95_exceeded",
        ),
        (deepseek_delta > 0, "deepseek_request_delta_nonzero"),
        (filter_ratio is not None and filter_ratio < 1.0, "filter_reasons_mismatch"),
        (matching_freezes < 1, "matching_freeze_missing"),
        (processing_errors > 0, "processing_errors_present"),
        (resource_failures > 0, "resource_limits_failed"),
        (selection_ratio is not None and selection_ratio < 1.0, "selected_codes_mismatch"),
        (
            local_p95 is not None and local_p95 > policy.local_publish_p95_seconds,
            "local_publish_p95_exceeded",
        ),
    )
    blockers = tuple(reason for blocked, reason in blocker_conditions if blocked)
    return TomorrowCutoverStatus(
        eligible=not blockers,
        blockers=blockers,
        sample_count=sample_count,
        successful_sample_count=successful_sample_count,
        trade_day_count=trade_day_count,
        selection_agreement_ratio=selection_ratio,
        filter_agreement_ratio=filter_ratio,
        local_publish_p95_seconds=local_p95,
        decision_age_p95_seconds=age_p95,
        processing_error_count=processing_errors,
        deepseek_request_delta=deepseek_delta,
        matching_freeze_count=matching_freezes,
        resource_failure_count=resource_failures,
    )


def _ratio(samples: tuple[TomorrowShadowObservation, ...], field: str) -> float | None:
    if not samples:
        return None
    matches = sum(bool(getattr(item, field)) for item in samples)
    return round(matches / len(samples), 6)


def _observation_identity(observation: TomorrowShadowObservation) -> tuple[date, str, str]:
    return (
        observation.trade_date,
        observation.baseline_snapshot_id,
        observation.input_version,
    )


def _nearest_rank_p95(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * 0.95))
    return round(ordered[rank - 1], 6)


def _require_shanghai(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tomorrow shadow observation time must be timezone-aware")
    if getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError("tomorrow shadow observation time must use Asia/Shanghai")


__all__ = [
    "TomorrowCutoverGate",
    "TomorrowCutoverPolicy",
    "TomorrowCutoverStatus",
    "TomorrowShadowObservation",
]

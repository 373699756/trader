"""Bounded engineering evidence for tomorrow v2 shadow cutover."""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time

from trader.application.ports.tomorrow_evidence import TomorrowShadowEvidencePort

_SHANGHAI_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class TomorrowCutoverPolicy:
    minimum_samples: int = 100
    minimum_trade_days: int = 1
    maximum_samples: int = 4096
    local_publish_p95_seconds: float = 5.0
    decision_age_p95_seconds: float = 10.0
    opening_sample_deadline: time = time(10, 0)
    freeze_sample_start: time = time(14, 50)

    def __post_init__(self) -> None:
        if min(self.minimum_samples, self.minimum_trade_days, self.maximum_samples) < 1:
            raise ValueError("tomorrow cutover sample limits must be positive")
        if self.minimum_samples > self.maximum_samples:
            raise ValueError("tomorrow minimum samples cannot exceed retained samples")
        for value in (self.local_publish_p95_seconds, self.decision_age_p95_seconds):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError("tomorrow cutover latency limits must be finite and positive")
        if self.opening_sample_deadline.tzinfo is not None or self.freeze_sample_start.tzinfo is not None:
            raise ValueError("tomorrow cutover policy times must be Shanghai wall-clock values")
        if self.opening_sample_deadline >= self.freeze_sample_start:
            raise ValueError("tomorrow opening evidence must precede freeze evidence")


@dataclass(frozen=True)
class TomorrowShadowObservation:
    trade_date: date
    observed_at: datetime
    baseline_snapshot_id: str
    decision_version: str
    input_version: str
    config_version: str
    strategy_version: str
    fusion_version: str
    decision_schema_version: str
    parent_decision_version: str
    selected_codes_match: bool
    filter_reasons_match: bool
    local_publish_seconds: float
    decision_age_seconds: float
    processing_seconds: float
    deepseek_request_delta: int
    resource_limits_passed: bool
    baseline_frozen: bool
    v2_frozen: bool
    freeze_codes_match: bool
    freeze_content_hash: str
    processing_error: str = ""

    def __post_init__(self) -> None:
        _require_shanghai(self.observed_at)
        if not all(
            value.strip()
            for value in (
                self.baseline_snapshot_id,
                self.decision_version,
                self.input_version,
                self.config_version,
                self.strategy_version,
                self.fusion_version,
                self.decision_schema_version,
            )
        ):
            raise ValueError("tomorrow shadow identities must not be empty")
        for value in (self.local_publish_seconds, self.decision_age_seconds, self.processing_seconds):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("tomorrow shadow latency values must be finite and non-negative")
        if self.deepseek_request_delta < 0:
            raise ValueError("tomorrow shadow DeepSeek request delta cannot be negative")
        if self.v2_frozen and not self.baseline_frozen:
            raise ValueError("tomorrow shadow v2 freeze cannot precede the baseline freeze")
        if self.freeze_codes_match and not (self.baseline_frozen and self.v2_frozen):
            raise ValueError("matching freeze codes require both chains to be frozen")
        if self.freeze_content_hash and not self.v2_frozen:
            raise ValueError("tomorrow shadow freeze hash requires a v2 freeze")
        if self.freeze_content_hash and (
            len(self.freeze_content_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.freeze_content_hash)
        ):
            raise ValueError("tomorrow shadow freeze hash must be lowercase SHA-256")


@dataclass(frozen=True)
class TomorrowCutoverStatus:
    eligible: bool
    blockers: tuple[str, ...]
    retained_sample_count: int
    sample_count: int
    successful_sample_count: int
    trade_day_count: int
    complete_trade_day_count: int
    evaluation_trade_date: str | None
    selection_agreement_ratio: float | None
    filter_agreement_ratio: float | None
    local_publish_p95_seconds: float | None
    decision_age_p95_seconds: float | None
    processing_error_count: int
    deepseek_request_delta: int
    matching_freeze_count: int
    resource_failure_count: int
    evidence_failure_count: int


class TomorrowCutoverGate:
    """Collects bounded shadow evidence without performing a cutover."""

    def __init__(
        self,
        policy: TomorrowCutoverPolicy | None = None,
        evidence: TomorrowShadowEvidencePort | None = None,
    ) -> None:
        self._policy = policy or TomorrowCutoverPolicy()
        self._evidence = evidence
        self._lock = threading.RLock()
        self._samples: deque[TomorrowShadowObservation] = deque()
        self._evidence_failure_count = 0

    def record(self, observation: TomorrowShadowObservation) -> None:
        with self._lock:
            if not self._record_memory(observation):
                return
            if self._evidence is None:
                return
            try:
                self._evidence.record(observation)
            except (OSError, RuntimeError, TypeError, ValueError):
                self._evidence_failure_count += 1

    def restore(self, observations: tuple[TomorrowShadowObservation, ...]) -> None:
        with self._lock:
            for observation in observations:
                self._record_memory(observation)

    def mark_evidence_failure(self) -> None:
        with self._lock:
            self._evidence_failure_count += 1

    def status(self) -> TomorrowCutoverStatus:
        with self._lock:
            samples = tuple(self._samples)
            evidence_failure_count = self._evidence_failure_count
        return _cutover_status(
            self._policy,
            samples,
            evidence_failure_count=evidence_failure_count,
        )

    def _record_memory(self, observation: TomorrowShadowObservation) -> bool:
        identity = _observation_identity(observation)
        for index, item in enumerate(self._samples):
            if _observation_identity(item) != identity:
                continue
            if observation.observed_at > item.observed_at:
                self._samples[index] = observation
                self._retain_recent()
                return True
            if observation.observed_at == item.observed_at and observation != item:
                self._evidence_failure_count += 1
            return False
        self._samples.append(observation)
        self._retain_recent()
        return any(_observation_identity(item) == identity for item in self._samples)

    def _retain_recent(self) -> None:
        ordered = sorted(
            self._samples,
            key=lambda item: (item.observed_at, _observation_identity(item)),
        )
        self._samples = deque(ordered[-self._policy.maximum_samples :])


def _cutover_status(
    policy: TomorrowCutoverPolicy,
    samples: tuple[TomorrowShadowObservation, ...],
    *,
    evidence_failure_count: int = 0,
) -> TomorrowCutoverStatus:
    retained_sample_count = len(samples)
    evaluation_samples, evaluation_trade_date = _evaluation_window(policy, samples)
    sample_count = len(evaluation_samples)
    successful = tuple(item for item in evaluation_samples if not item.processing_error)
    successful_sample_count = len(successful)
    trade_day_count = len({item.trade_date for item in successful})
    complete_trade_day_count = _complete_trade_day_count(policy, successful)
    selection_ratio = _ratio(successful, "selected_codes_match")
    filter_ratio = _ratio(successful, "filter_reasons_match")
    local_p95 = _nearest_rank_p95(tuple(item.local_publish_seconds for item in successful))
    age_p95 = _nearest_rank_p95(tuple(item.decision_age_seconds for item in successful))
    processing_errors = sum(bool(item.processing_error) for item in evaluation_samples)
    deepseek_delta = sum(item.deepseek_request_delta for item in evaluation_samples)
    matching_freezes = sum(
        item.baseline_frozen and item.v2_frozen and item.freeze_codes_match and bool(item.freeze_content_hash)
        for item in successful
    )
    resource_failures = sum(not item.resource_limits_passed for item in evaluation_samples)
    blocker_conditions = (
        (successful_sample_count < policy.minimum_samples, "insufficient_samples"),
        (complete_trade_day_count < policy.minimum_trade_days, "incomplete_trade_day"),
        (
            age_p95 is not None and age_p95 > policy.decision_age_p95_seconds,
            "decision_age_p95_exceeded",
        ),
        (deepseek_delta > 0, "deepseek_request_delta_nonzero"),
        (evidence_failure_count > 0, "evidence_persistence_failed"),
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
        retained_sample_count=retained_sample_count,
        sample_count=sample_count,
        successful_sample_count=successful_sample_count,
        trade_day_count=trade_day_count,
        complete_trade_day_count=complete_trade_day_count,
        evaluation_trade_date=evaluation_trade_date.isoformat() if evaluation_trade_date is not None else None,
        selection_agreement_ratio=selection_ratio,
        filter_agreement_ratio=filter_ratio,
        local_publish_p95_seconds=local_p95,
        decision_age_p95_seconds=age_p95,
        processing_error_count=processing_errors,
        deepseek_request_delta=deepseek_delta,
        matching_freeze_count=matching_freezes,
        resource_failure_count=resource_failures,
        evidence_failure_count=evidence_failure_count,
    )


def _evaluation_window(
    policy: TomorrowCutoverPolicy,
    samples: tuple[TomorrowShadowObservation, ...],
) -> tuple[tuple[TomorrowShadowObservation, ...], date | None]:
    successful = tuple(item for item in samples if not item.processing_error)
    complete_dates = _complete_trade_dates(policy, successful)
    if not complete_dates:
        return samples, None
    latest_complete = max(complete_dates)
    return tuple(item for item in samples if item.trade_date == latest_complete), latest_complete


def _complete_trade_day_count(
    policy: TomorrowCutoverPolicy,
    samples: tuple[TomorrowShadowObservation, ...],
) -> int:
    return len(_complete_trade_dates(policy, samples))


def _complete_trade_dates(
    policy: TomorrowCutoverPolicy,
    samples: tuple[TomorrowShadowObservation, ...],
) -> tuple[date, ...]:
    complete: list[date] = []
    trade_dates = sorted({item.trade_date for item in samples})
    for trade_date in trade_dates:
        day = tuple(item for item in samples if item.trade_date == trade_date)
        has_opening = any(
            item.observed_at.date() == trade_date and item.observed_at.time() <= policy.opening_sample_deadline
            for item in day
        )
        has_freeze = any(
            item.observed_at.date() == trade_date
            and item.observed_at.time() >= policy.freeze_sample_start
            and item.baseline_frozen
            and item.v2_frozen
            and item.freeze_codes_match
            and bool(item.freeze_content_hash)
            for item in day
        )
        if has_opening and has_freeze:
            complete.append(trade_date)
    return tuple(complete)


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

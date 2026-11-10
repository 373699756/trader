"""Bounded live batch evidence for the existing Web health diagnostic."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, NoReturn
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from scripts.recommendation.web.complete_round_stream import EventStreamDecoder, EventStreamFrame
from scripts.recommendation.web.web_health_contract import WebPipelineStageSnapshot, parse_web_sample
from trader.infra.http_contracts import HttpCancelled, HttpError, HttpRequest, HttpTimeout, HttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES
from trader.recommendation.domain.market.quote_freshness import QuoteFreshnessAgeSeconds, QuoteSessionTimes
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_STRATEGIES = ("tomorrow", "d25")
_MAX_ROUND_RECORDS = 64
_MAX_STREAM_BYTES = 64 * 1024 * 1024
_AGE_LIMIT_SECONDS = 30.0
_StageOutcome = Literal["pending", "matched", "source_age_unqualified", "superseded", "identity_mismatch"]


class ObservationFetchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("observation_http_fetch_failed")


class _Stopped(RuntimeError):
    pass


@dataclass(frozen=True)
class _HeadIdentity:
    profile: str
    strategy: str
    stage: str
    publication_id: str
    projection_version: str
    observed_at: datetime
    trade_date: str
    generation: int
    input_versions: tuple[tuple[str, str], ...]
    items: tuple[tuple[str, int, datetime], ...]


@dataclass(frozen=True)
class _HeadEvidence:
    identity: _HeadIdentity
    api_received_at: datetime
    decision_age_max_seconds: float | None
    event_delivery_age_max_seconds: float | None
    api_delivery_age_max_seconds: float | None
    selected_count: int
    evaluated_count: int | None
    stages: tuple[WebPipelineStageSnapshot, ...]


@dataclass(frozen=True)
class _RoundEvidence:
    sequence: int
    event_id: int
    generation: int
    event_received_at: datetime
    expected: tuple[_HeadIdentity, ...]
    outcome: _StageOutcome = "pending"
    reason: str = "awaiting_atomic_snapshot"
    heads: tuple[_HeadEvidence, ...] = ()
    atomic_heads: tuple[_HeadIdentity, ...] = ()
    scheduler_elapsed_ms: float | None = None


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(code)
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError(code)
    return value


def _integer(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(code)
    return value


def _timestamp(value: object, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, code))
    except ValueError as exc:
        raise ValueError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(code)
    return parsed.astimezone(_SHANGHAI)


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _head_identity(
    payload: Mapping[str, object], profile: str, strategy: str, *, event: bool,
) -> _HeadIdentity:
    if payload.get("scoring_profile") != profile or payload.get("strategy") != strategy:
        raise ValueError("head_coordinates_invalid")
    if payload.get("run_mode") == "preview" or payload.get("frozen") is not False:
        raise ValueError("preview_or_frozen_head")
    stage = payload.get("stage")
    if (stage != "local" if event else stage not in {"local", "hybrid"}) or (
        not event and payload.get("status") != "ready"
    ):
        raise ValueError("head_not_local_ready")
    versions = _mapping(payload.get("input_versions"), "input_versions_missing")
    if not 1 <= len(versions) <= 32:
        raise ValueError("input_versions_invalid")
    inputs = tuple(sorted((key, _text(value, "input_version_invalid")) for key, value in versions.items()))
    raw_generation = versions.get("session_generation")
    if (
        not isinstance(raw_generation, str) or not raw_generation.isascii()
        or not raw_generation.isdecimal() or len(raw_generation) > 12
    ):
        raise ValueError("session_generation_missing")
    items = payload.get("upserts" if event else "items")
    if not isinstance(items, list) or len(items) != 6:
        raise ValueError("head_six_seats_missing")
    identities: list[tuple[str, int, datetime]] = []
    for item in items:
        row = _mapping(item, "head_item_invalid")
        quote = row if event else _mapping(row.get("quote"), "head_quote_missing")
        if row.get("selected") is not True:
            raise ValueError("head_selected_flag_invalid")
        identities.append((
            _text(row.get("code"), "head_code_missing"),
            _integer(row.get("rank"), "head_rank_invalid"),
            _timestamp(quote.get("source_time"), "head_source_time_invalid"),
        ))
    if len({item[0] for item in identities}) != 6 or tuple(item[1] for item in identities) != (1, 2, 3, 4, 5, 6):
        raise ValueError("head_ranks_invalid")
    coverage = _mapping(payload.get("coverage"), "head_coverage_missing")
    if coverage.get("selected_count") != 6:
        raise ValueError("head_coverage_invalid")
    observed_at = _timestamp(payload.get("published_at" if event else "observed_at"), "head_observation_invalid")
    trade_date = _text(payload.get("trade_date"), "head_trade_date_missing")
    if observed_at.date().isoformat() != trade_date:
        raise ValueError("head_trade_date_invalid")
    return _HeadIdentity(
        profile, strategy, str(stage),
        _text(payload.get("version" if event else "freeze_version"), "publication_identity_missing"),
        _text(payload.get("projection_version"), "projection_identity_missing"),
        observed_at, trade_date, int(raw_generation), inputs, tuple(identities),
    )


def _batch_heads(payload: Mapping[str, object], *, event: bool) -> tuple[tuple[_HeadIdentity, Mapping[str, object]], ...]:
    if payload.get("schema_version") != ("freeze_event" if event else "freeze_current_batch"):
        raise ValueError("batch_schema_invalid")
    profiles = _mapping(payload.get("profiles"), "batch_profiles_missing")
    if set(profiles) != set(SCORING_PROFILE_IDS):
        raise ValueError("batch_profiles_incomplete")
    heads: list[tuple[_HeadIdentity, Mapping[str, object]]] = []
    for profile in SCORING_PROFILE_IDS:
        strategies = _mapping(profiles[profile], "batch_strategies_missing")
        if set(strategies) != set(_STRATEGIES):
            raise ValueError("batch_strategies_incomplete")
        for strategy in _STRATEGIES:
            raw = _mapping(strategies[strategy], "batch_head_invalid")
            heads.append((_head_identity(raw, profile, strategy, event=event), raw))
    if len({head.trade_date for head, _ in heads}) != 1 or len({head.generation for head, _ in heads}) != 1:
        raise ValueError("batch_coordinates_inconsistent")
    return tuple(heads)


def _age_max(
    source_times: tuple[datetime, ...], observed_at: datetime, *, decision: bool, sessions: QuoteSessionTimes,
) -> float | None:
    values = tuple(
        QuoteFreshnessAgeSeconds(source, observed_at, sessions=sessions)
        if decision else (observed_at - source).total_seconds()
        for source in source_times
    )
    return max(values) if values and all(math.isfinite(value) and value >= 0 for value in values) else None


def _head_evidence(
    identity: _HeadIdentity, payload: Mapping[str, object], event_received_at: datetime, api_received_at: datetime,
    sessions: QuoteSessionTimes,
) -> _HeadEvidence:
    sample = parse_web_sample(1, api_received_at.isoformat(), status_payload=None, freeze_payloads={identity.strategy: payload})
    projection = sample.freezes[identity.strategy]
    pipeline = projection.pipeline
    if (
        pipeline is None or pipeline.invalid_fields or not pipeline.committed_audit.observed
        or pipeline.committed_audit.invalid_fields or pipeline.committed_audit.stage_count != len(PIPELINE_STAGES)
    ):
        raise ValueError("committed_eleven_stage_audit_invalid")
    sources = tuple(item[2] for item in identity.items)
    return _HeadEvidence(
        identity, api_received_at,
        _age_max(sources, identity.observed_at, decision=True, sessions=sessions),
        _age_max(sources, event_received_at, decision=False, sessions=sessions),
        _age_max(sources, api_received_at, decision=False, sessions=sessions),
        6, projection.coverage.evaluated_count, pipeline.committed_audit.stages,
    )


class _Observation:
    def __init__(
        self,
        base_url: str,
        required_rounds: int,
        observation_seconds: float,
        interval_seconds: float,
        timeout_seconds: float,
        profile: str,
        fetch_mapping: Callable[[str, float], Mapping[str, object]],
        monotonic: Callable[[], float],
        wall_clock: Callable[[], datetime],
    ) -> None:
        self.base_url = base_url
        self.required = required_rounds
        self.duration = observation_seconds
        self.interval = interval_seconds
        self.timeout = timeout_seconds
        self.profile = profile
        self.fetch_mapping = fetch_mapping
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.started = monotonic()
        self.deadline = self.started + observation_seconds
        self.runtime_version = ""
        self.sessions: QuoteSessionTimes | None = None
        self.generation: int | None = None
        self.initial_cursor = self.last_cursor = 0
        self.last_poll = -math.inf
        self.rounds: list[_RoundEvidence] = []
        self.seen: set[tuple[str, int, int]] = set()
        self.last_published_sequence: int | None = None
        self.last_qualified_sequence: int | None = None
        self.current_qualified_streak = 0
        self.maximum_qualified_streak = 0
        self.pending: int | None = None
        self.counts: Counter[str] = Counter()
        self.reasons: Counter[str] = Counter()
        self.stop_reason = ""
        self.latest_round: tuple[int | None, str | None] = (None, None)
        self.latest_phase: str | None = None
        self.decoder = EventStreamDecoder()

    def stop(self, reason: str) -> NoReturn:
        self.stop_reason = reason
        raise _Stopped(reason)

    def break_streak(self) -> None:
        self.current_qualified_streak = 0
        self.last_qualified_sequence = None

    def reject_batch(self) -> None:
        self.break_streak()
        self.reasons["incomplete_or_invalid_batch"] += 1
        if self.pending is not None:
            self.rounds[self.pending] = replace(
                self.rounds[self.pending], outcome="superseded", reason="invalid_batch_before_poll",
            )
            self.pending = None

    def read(self, path: str) -> Mapping[str, object]:
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            self.stop("observation_deadline")
        return self.fetch_mapping(self.base_url + path, min(self.timeout, remaining))

    def status(self) -> Mapping[str, object]:
        payload = self.read("/api/status?" + urlencode({"profile": self.profile}))
        runtime = _text(payload.get("runtime_version"), "runtime_identity_missing")
        if self.runtime_version and runtime != self.runtime_version:
            self.stop("runtime_identity_changed")
        self.runtime_version = runtime
        if payload.get("run_mode") == "preview":
            self.stop("preview_runtime")
        profiles = payload.get("available_profiles")
        if not isinstance(profiles, list) or any(not isinstance(value, str) for value in profiles) or set(profiles) != set(SCORING_PROFILE_IDS):
            self.stop("required_profiles_unavailable")
        events = _mapping(payload.get("events"), "event_status_missing")
        cursor = _integer(events.get("sequence"), "event_sequence_missing")
        if cursor < self.initial_cursor:
            self.stop("runtime_event_sequence_regressed")
        scheduler = _mapping(payload.get("scheduler"), "scheduler_status_missing")
        cadence = _mapping(scheduler.get("cadence"), "cadence_status_missing")
        schedule = _mapping(cadence.get("market_sessions"), "market_sessions_missing")
        sessions = QuoteSessionTimes(
            _integer(schedule.get("morning_open"), "market_sessions_invalid"),
            _integer(schedule.get("morning_close"), "market_sessions_invalid"),
            _integer(schedule.get("afternoon_open"), "market_sessions_invalid"),
            _integer(schedule.get("afternoon_close"), "market_sessions_invalid"),
        )
        if self.sessions is not None and self.sessions != sessions:
            self.stop("runtime_market_sessions_changed")
        self.sessions = sessions
        current = _mapping(scheduler.get("recommendation_round"), "round_status_missing")
        current_sequence = current.get("sequence")
        current_outcome = current.get("outcome")
        self.latest_round = (
            current_sequence if isinstance(current_sequence, int) and not isinstance(current_sequence, bool) else None,
            current_outcome if isinstance(current_outcome, str) else None,
        )
        phase = payload.get("phase")
        self.latest_phase = phase if isinstance(phase, str) else None
        return payload

    def begin(self) -> None:
        payload = self.status()
        self.initial_cursor = self.last_cursor = _integer(
            _mapping(payload.get("events"), "event_status_missing").get("sequence"), "event_sequence_missing",
        )
        self.last_poll = self.monotonic()

    def receive_headers(self, status: int, headers: tuple[tuple[str, str], ...]) -> None:
        content_type = next((value for key, value in headers if key.lower() == "content-type"), "")
        if status != 200 or not content_type.lower().startswith("text/event-stream"):
            self.stop("event_stream_response_invalid")

    def receive(self, chunk: bytes) -> None:
        try:
            for frame in self.decoder.feed(chunk):
                self.frame(frame)
            self.poll()
        except ValueError:
            self.stop("event_stream_protocol_invalid")

    def frame(self, frame: EventStreamFrame) -> None:
        if frame.event_id is None:
            self.stop("event_identity_missing")
        if frame.event_type == "resync_required":
            self.stop("event_stream_resync_required")
        if frame.event_type not in {"batch", "freeze", "overlay"}:
            self.stop("event_type_unsupported")
        if frame.event_id <= self.initial_cursor:
            self.counts["initial_replay_events"] += 1
            return
        if frame.event_id <= self.last_cursor:
            self.counts["duplicate_events"] += 1
            return
        self.last_cursor = frame.event_id
        if frame.event_type != "batch":
            return
        self.counts["observed_batch_events"] += 1
        try:
            payload = _mapping(json.loads(frame.data), "event_json_invalid")
            sequence = _integer(payload.get("round_sequence"), "published_round_sequence_missing")
        except (ValueError, TypeError, RecursionError):
            self.reject_batch()
            return
        if self.last_published_sequence is not None:
            if sequence < self.last_published_sequence:
                self.break_streak()
                self.stop("published_round_sequence_regressed")
            if sequence == self.last_published_sequence:
                self.counts["duplicate_rounds"] += 1
                return
            if sequence != self.last_published_sequence + 1:
                self.break_streak()
                self.reasons["published_round_sequence_gap"] += 1
        self.last_published_sequence = sequence
        try:
            heads = tuple(identity for identity, _ in _batch_heads(payload, event=True))
        except (ValueError, TypeError, RecursionError):
            self.reject_batch()
            return
        generation = heads[0].generation
        if self.generation is not None and generation != self.generation:
            self.stop("session_generation_changed")
        self.generation = generation
        identity = (self.runtime_version, generation, sequence)
        if identity in self.seen:
            self.counts["duplicate_rounds"] += 1
            return
        self.seen.add(identity)
        self.counts["complete_published_rounds"] += 1
        if len(self.rounds) >= _MAX_ROUND_RECORDS:
            self.stop("round_record_limit")
        if self.pending is not None:
            self.break_streak()
            self.rounds[self.pending] = replace(
                self.rounds[self.pending], outcome="superseded", reason="newer_batch_before_poll",
            )
            self.reasons["newer_batch_before_poll"] += 1
        self.rounds.append(_RoundEvidence(sequence, frame.event_id, generation, self.wall_clock(), heads))
        self.pending = len(self.rounds) - 1

    def poll(self) -> None:
        if self.monotonic() >= self.deadline:
            self.stop("observation_deadline")
        if self.monotonic() - self.last_poll < self.interval:
            return
        if self.pending is None:
            return
        self.last_poll = self.monotonic()
        try:
            atomic = self.read("/api/freezes/current")
            received_at = self.wall_clock()
            status = self.status()
        except ObservationFetchError as exc:
            self.reasons[exc.code] += 1
            return
        self.counts["http_poll_samples"] += 1
        index = self.pending
        pending = self.rounds[index]
        try:
            actual = _batch_heads(atomic, event=False)
            identities = tuple(identity for identity, _ in actual)
            if identities != pending.expected:
                advanced = any(
                    head.observed_at > expected.observed_at or (
                        head.observed_at == expected.observed_at and head.stage == "hybrid"
                        and head.publication_id != expected.publication_id
                    )
                    for head, expected in zip(identities, pending.expected, strict=True)
                )
                outcome: _StageOutcome = "superseded" if advanced else "identity_mismatch"
                reason = "atomic_snapshot_advanced" if advanced else "atomic_identity_mismatch"
                self.rounds[index] = replace(pending, outcome=outcome, reason=reason, atomic_heads=identities)
                self.reasons[reason] += 1
                self.break_streak()
                self.pending = None
                return
            if self.sessions is None:
                self.stop("market_sessions_missing")
            evidence = tuple(
                _head_evidence(identity, raw, pending.event_received_at, received_at, self.sessions)
                for identity, raw in actual
            )
        except ValueError:
            self.break_streak()
            self.reasons["atomic_snapshot_incomplete_or_invalid"] += 1
            return
        self.counts["identity_matched_rounds"] += 1
        decision_qualified = all(
            head.decision_age_max_seconds is not None and head.decision_age_max_seconds <= _AGE_LIMIT_SECONDS
            for head in evidence
        )
        delivery_qualified = all(
            head.api_delivery_age_max_seconds is not None and head.api_delivery_age_max_seconds <= _AGE_LIMIT_SECONDS
            for head in evidence
        )
        self.counts["decision_source_age_qualified_rounds"] += int(decision_qualified)
        self.counts["delivery_source_age_qualified_rounds"] += int(delivery_qualified)
        qualified = decision_qualified and delivery_qualified
        self.counts["source_age_qualified_rounds"] += int(qualified)
        if qualified:
            self.current_qualified_streak = (
                self.current_qualified_streak + 1
                if self.last_qualified_sequence is not None and pending.sequence == self.last_qualified_sequence + 1
                else 1
            )
            self.last_qualified_sequence = pending.sequence
            self.maximum_qualified_streak = max(self.maximum_qualified_streak, self.current_qualified_streak)
        else:
            self.break_streak()
        current = _mapping(_mapping(status.get("scheduler"), "scheduler_status_missing").get("recommendation_round"), "round_status_missing")
        elapsed = _number(current.get("elapsed_ms")) if (
            current.get("sequence") == pending.sequence and current.get("outcome") == "published"
        ) else None
        self.rounds[index] = replace(
            pending, outcome="matched" if qualified else "source_age_unqualified",
            reason="accepted" if qualified else "source_age_exceeded_or_missing", heads=evidence,
            atomic_heads=identities,
            scheduler_elapsed_ms=elapsed,
        )
        self.pending = None
        if not qualified:
            self.reasons["source_age_exceeded_or_missing"] += 1
        if self.current_qualified_streak >= self.required:
            self.stop("target_reached")

    def report(self) -> dict[str, object]:
        return {
            "schema_version": "web_recommendation_health",
            "mode": "complete_rounds",
            "status": "passed" if self.stop_reason == "target_reached" else "failed",
            "collected_at": self.wall_clock().isoformat(),
            "summary": {
                "required_complete_rounds": self.required,
                "current_consecutive_qualified_rounds": self.current_qualified_streak,
                "maximum_consecutive_qualified_rounds": self.maximum_qualified_streak,
                "round_target_policy": "consecutive_published_sequence_and_qualified_atomic_heads",
                **{key: self.counts[key] for key in (
                    "observed_batch_events", "complete_published_rounds", "identity_matched_rounds",
                    "decision_source_age_qualified_rounds", "delivery_source_age_qualified_rounds",
                    "source_age_qualified_rounds", "duplicate_rounds", "duplicate_events",
                    "initial_replay_events", "http_poll_samples",
                )},
                "stop_reason": self.stop_reason,
                "scope": "live_sse_batch_atomic_api_eleven_stage_source_age_only",
                "performance_acceptance": False,
                "browser_acceptance": False,
                "freeze_recovery_acceptance": False,
            },
            "configuration": {
                "observation_seconds": self.duration, "minimum_poll_interval_seconds": self.interval,
                "status_profile": self.profile, "quote_age_limit_seconds": _AGE_LIMIT_SECONDS,
                "required_age_scope": "decision_and_api_delivery", "maximum_round_records": _MAX_ROUND_RECORDS,
                "maximum_stream_bytes": _MAX_STREAM_BYTES,
                "market_sessions": {
                    "morning_open": self.sessions.morning_open,
                    "morning_close": self.sessions.morning_close,
                    "afternoon_open": self.sessions.afternoon_open,
                    "afternoon_close": self.sessions.afternoon_close,
                } if self.sessions is not None else None,
            },
            "runtime_identity": {
                "runtime_version": self.runtime_version, "session_generation": self.generation,
                "initial_event_cursor": self.initial_cursor, "last_event_cursor": self.last_cursor,
            },
            "latest_runtime": {
                "phase": self.latest_phase, "round_sequence": self.latest_round[0],
                "round_outcome": self.latest_round[1],
            },
            "reasons": [{"code": code, "count": count} for code, count in self.reasons.most_common(32)],
            "rounds": [_round_payload(item) for item in self.rounds],
        }


def _round_payload(item: _RoundEvidence) -> dict[str, object]:
    return {
        "round_sequence": item.sequence, "event_id": item.event_id, "session_generation": item.generation,
        "event_received_at": item.event_received_at.isoformat(), "outcome": item.outcome, "reason": item.reason,
        "scheduler_elapsed_ms": item.scheduler_elapsed_ms,
        "published_heads": [_identity_payload(head) for head in item.expected],
        "atomic_heads": [_identity_payload(head) for head in item.atomic_heads],
        "heads": [{
            **_identity_payload(head.identity),
            "api_received_at": head.api_received_at.isoformat(), "selected_count": head.selected_count,
            "evaluated_count": head.evaluated_count,
            "quote_source_min": min(value[2] for value in head.identity.items).isoformat(),
            "quote_source_max": max(value[2] for value in head.identity.items).isoformat(),
            "decision_source_age_max_seconds": head.decision_age_max_seconds,
            "event_delivery_source_age_max_seconds": head.event_delivery_age_max_seconds,
            "api_delivery_source_age_max_seconds": head.api_delivery_age_max_seconds,
            "stages": [{
                "key": stage.key, "input_count": stage.input_count, "output_count": stage.output_count,
                "rejected_count": stage.rejected_count, "pending_count": stage.pending_count,
                "failed_count": stage.failed_count, "not_selected_count": stage.not_selected_count,
                "duration_ms": stage.duration_ms,
                "operation_durations": [{"operation": key, "duration_ms": value} for key, value in stage.operation_durations],
            } for stage in head.stages],
        } for head in item.heads],
    }


def _identity_payload(head: _HeadIdentity) -> dict[str, object]:
    return {
        "profile": head.profile, "strategy": head.strategy, "stage": head.stage,
        "publication_id": head.publication_id, "projection_version": head.projection_version,
        "observed_at": head.observed_at.isoformat(), "trade_date": head.trade_date,
        "input_identity_sha256": CanonicalDigest(head.input_versions),
        "selected_codes_sha256": CanonicalDigest(tuple((code, rank) for code, rank, _source in head.items)),
    }


def ObserveCompleteRounds(
    base_url: str,
    *,
    required_rounds: int,
    observation_seconds: float,
    interval_seconds: float,
    timeout_seconds: float,
    scoring_profile: str | None,
    fetch_mapping: Callable[[str, float], Mapping[str, object]],
    transport: HttpTransport,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(_SHANGHAI),
) -> dict[str, object]:
    if (
        type(required_rounds) is not int or not 1 <= required_rounds <= 20
        or not math.isfinite(observation_seconds) or not 0 < observation_seconds <= 900
        or not math.isfinite(interval_seconds) or interval_seconds < 15
        or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        or (scoring_profile is not None and scoring_profile not in SCORING_PROFILE_IDS)
    ):
        raise ValueError("complete_round_observation_options_invalid")
    observer = _Observation(
        base_url, required_rounds, observation_seconds, interval_seconds, timeout_seconds,
        scoring_profile or "v2", fetch_mapping, monotonic, wall_clock,
    )
    try:
        observer.begin()
        transport.execute(HttpRequest(
            "GET", base_url + "/api/events?" + urlencode({"profile": observer.profile}), observation_seconds,
            headers=(("Accept", "text/event-stream"), ("Last-Event-ID", str(observer.initial_cursor))),
            deadline=observer.deadline, max_response_bytes=_MAX_STREAM_BYTES,
            cancel_requested=lambda: bool(observer.stop_reason), io_timeout_seconds=min(30.0, observation_seconds),
            receive_headers=observer.receive_headers, receive_chunk=observer.receive,
        ))
        observer.stop_reason = "event_stream_closed_before_target"
    except _Stopped:
        pass
    except HttpTimeout:
        observer.stop_reason = "observation_deadline" if monotonic() >= observer.deadline else "event_stream_timeout"
    except HttpCancelled:
        observer.stop_reason = observer.stop_reason or "event_stream_cancelled"
    except (HttpError, ObservationFetchError):
        observer.stop_reason = "event_stream_or_initial_http_failed"
    except (TypeError, ValueError):
        observer.stop_reason = "initial_observation_contract_invalid"
    return observer.report()


__all__ = ["ObservationFetchError", "ObserveCompleteRounds"]

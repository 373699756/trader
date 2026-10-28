"""Strict JSON boundary, rejecting ambiguous or incomplete budget evidence."""

from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from trader.infra.serialization.canonical_codec import CanonicalDigest, CanonicalValue, EncodeCanonicalValue
from trader.recommendation.infra.deepseek.budget_state import BudgetBatch, BudgetHealth, BudgetRequest, BudgetState

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 4 * 1024 * 1024
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def EncodeBudgetState(state: BudgetState) -> CanonicalValue:
    payload = EncodeCanonicalValue(state)
    assert isinstance(payload, dict)
    return {**payload, "state_hash": CanonicalDigest(payload)}


def DecodeBudgetState(value: object, identity: str) -> BudgetState:
    data = _object(value)
    content = {key: item for key, item in data.items() if key != "state_hash"}
    if data.get("state_hash") != CanonicalDigest(content):
        raise ValueError("budget_state_hash_mismatch")
    if _integer(data, "schema_version") != SCHEMA_VERSION or _text(data, "owner_identity") != identity:
        raise ValueError("budget_state_identity_unsupported")
    requests = tuple(_request(item) for item in _array(data, "requests"))
    batches = tuple(_batch(item) for item in _array(data, "review_batches"))
    health = _object(data["health_state"])
    mode = _text(health, "mode", {"closed", "open", "half_open", "recovering"})
    result = BudgetState(
        SCHEMA_VERSION,
        date.fromisoformat(_text(data, "budget_date")),
        _text(data, "budget_policy_hash"),
        _integer(data, "revision"),
        identity,
        date.fromisoformat(_text(data, "blocked_date")) if data["blocked_date"] is not None else None,
        _text(data, "blocked_reason"),
        requests,
        batches,
        BudgetHealth(
            mode,
            _timestamp(health, "open_until", optional=True),
            _text(health, "probe_batch_id"),
            _integer(health, "recovery_successes"),
            _text(health, "reason"),
        ),
    )
    if len(requests) > 336 or len(batches) > 336:
        raise ValueError("budget_state_capacity_exceeded")
    if len({item.request_id for item in requests}) != len(requests):
        raise ValueError("budget_duplicate_request")
    if len({item.batch_id for item in batches}) != len(batches):
        raise ValueError("budget_duplicate_batch")
    for day in {item.budget_date for item in requests}:
        if day > result.budget_date or sum(item.budget_date == day for item in requests) > 168:
            raise ValueError("budget_request_day_invalid")
    for batch in batches:
        if batch.budget_date > result.budget_date or not any(item.batch_id == batch.batch_id for item in requests):
            raise ValueError("budget_batch_without_request")
    batch_ids = {batch.batch_id for batch in batches}
    if any(item.batch_id and item.batch_id not in batch_ids for item in requests):
        raise ValueError("budget_request_without_batch")
    if mode in {"open", "half_open"} and result.health_state.open_until is None:
        raise ValueError("budget_health_deadline_missing")
    if mode == "half_open" and result.health_state.probe_batch_id not in batch_ids:
        raise ValueError("budget_probe_missing")
    return result


def BudgetDay(value: datetime) -> date:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("budget time must be timezone-aware")
    return value.astimezone(_SHANGHAI).date()


def _request(value: object) -> BudgetRequest:
    data = _object(value)
    requested = _required_timestamp(data, "requested_at")
    day = date.fromisoformat(_text(data, "budget_date"))
    if day != BudgetDay(requested):
        raise ValueError("budget_request_day_mismatch")
    status = _text(data, "status", {"reserved", "success", "failed", "abandoned"})
    completed = _timestamp(data, "completed_at", optional=True)
    if (status == "reserved") != (completed is None):
        raise ValueError("budget_request_completion_mismatch")
    return BudgetRequest(
        _text(data, "request_id"), day, _text(data, "profile", {"v1", "v2", "v3", "v5"}),
        _text(data, "strategy", {"tomorrow", "d25"}), _text(data, "bucket"),
        _text(data, "stage"), _text(data, "batch_id"), _text(data, "emergency_reason"), requested,
        status, completed, _integer(data, "http_status") if data["http_status"] is not None else None,
        _integer(data, "token_count"), _boolean(data, "timed_out"),
    )


def _batch(value: object) -> BudgetBatch:
    data = _object(value)
    requested = _required_timestamp(data, "requested_at")
    day = date.fromisoformat(_text(data, "budget_date"))
    if day != BudgetDay(requested):
        raise ValueError("budget_batch_day_mismatch")
    batch = BudgetBatch(
        _text(data, "batch_id"), day, _text(data, "profile", {"v1", "v2", "v3", "v5"}),
        _text(data, "strategy", {"tomorrow", "d25"}), requested, _required_timestamp(data, "deadline"),
        _integer(data, "candidate_count"), _integer(data, "cache_hit_count"),
        _text(data, "status", {"running", "success", "partial", "failed", "skipped", "abandoned"}),
        _timestamp(data, "completed_at", optional=True), _integer(data, "applied"), _integer(data, "abstain"),
        _integer(data, "rejected"), _integer(data, "late"),
    )
    outcomes = batch.applied + batch.abstain + batch.rejected + batch.late
    if batch.cache_hit_count > batch.candidate_count or outcomes > batch.candidate_count:
        raise ValueError("budget_batch_counts_invalid")
    if (batch.status == "running") != (batch.completed_at is None):
        raise ValueError("budget_batch_completion_mismatch")
    return batch


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("budget_json_object_required")
    return cast(dict[str, object], value)


def _array(data: dict[str, object], key: str) -> list[object]:
    value = data[key]
    if not isinstance(value, list):
        raise ValueError("budget_json_array_required")
    return value


def _text(data: dict[str, object], key: str, choices: set[str] | None = None) -> str:
    value = data[key]
    if not isinstance(value, str) or len(value) > 256 or (choices is not None and value not in choices):
        raise ValueError("budget_json_text_invalid")
    return value


def _integer(data: dict[str, object], key: str) -> int:
    value = data[key]
    if type(value) is not int or value < 0:
        raise ValueError("budget_json_integer_invalid")
    return value


def _boolean(data: dict[str, object], key: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise ValueError("budget_json_boolean_invalid")
    return value


def _timestamp(data: dict[str, object], key: str, *, optional: bool = False) -> datetime | None:
    if optional and data[key] is None:
        return None
    value = datetime.fromisoformat(_text(data, key))
    BudgetDay(value)
    return value


def _required_timestamp(data: dict[str, object], key: str) -> datetime:
    result = _timestamp(data, key)
    assert result is not None
    return result


__all__ = ["SCHEMA_VERSION", "MAX_STATE_BYTES", "BudgetDay", "EncodeBudgetState", "DecodeBudgetState"]

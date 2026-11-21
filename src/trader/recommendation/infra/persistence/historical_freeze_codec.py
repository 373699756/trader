"""Verify original saved hashes and project original fields without current adoption."""

import json
from datetime import date, datetime, timedelta
from typing import cast

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.publication.freeze_identity import FreezeStage, PredictionTargetSuffix
from trader.recommendation.domain.publication.historical_freeze import HistoricalFreezeRecord, HistoricalFreezeSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_record_codec import DecodeFreezeItem, DecodeFreezePipeline, DecodeSelectionDiagnostics


def HistoricalRecordFromBytes(payload: bytes) -> HistoricalFreezeRecord:
    """One read projection, independent of historical format labels and current writers."""
    raw = json.loads(payload)
    if not isinstance(raw, dict) or not isinstance(raw.get("freeze"), dict):
        raise ValueError("historical freeze object missing")
    saved = raw["freeze"]
    if set(raw) != {"freeze", "freeze_version", "freeze_hash", "committed_at", "commit_kind", "payload_hash", "version"} | ({"schema_version"} if "schema_version" in raw else set()):
        raise ValueError("historical record unknown fields")
    allowed = {"strategy", "trade_date", "sequence", "observed_at", "stage", "parent_version", "input_versions",
               "config_version", "strategy_version", "items", "filter_aggregates", "degraded_reasons", "selection_diagnostics",
               "population_count", "rejected_count", "pipeline", "is_formal", "schema_version", "fusion_version"}
    if set(saved) - allowed:
        raise ValueError("historical freeze unknown fields")
    freeze_hash = _text(raw, "freeze_hash")
    record_hash = _text(raw, "payload_hash")
    if CanonicalDigest(saved) != freeze_hash or CanonicalDigest({
        key: value for key, value in raw.items() if key not in {"payload_hash", "version"}
    }) != record_hash:
        raise ValueError("historical freeze original hash mismatch")
    strategy = Strategy(_text(saved, "strategy"))
    trade_date = date.fromisoformat(_text(saved, "trade_date"))
    observed_at = _time(saved, "observed_at")
    committed_at = _time(raw, "committed_at")
    sequence = saved.get("sequence")
    stage = _text(saved, "stage")
    if (strategy not in {Strategy.TOMORROW, Strategy.D25} or type(sequence) is not int or sequence < 0
        or stage not in {"local", "hybrid"} or observed_at.date() != trade_date
        or committed_at.date() != trade_date or committed_at < observed_at
        or raw.get("commit_kind") not in {"scheduled", "close_fallback"}):
        raise ValueError("historical freeze coordinates invalid")
    freeze_version = _text(raw, "freeze_version")
    coordinate_identity = f"freeze:{strategy.value}:{trade_date.isoformat()}:{stage}:{sequence}"
    input_versions = _pairs(saved, "input_versions")
    versions = dict(input_versions)
    prediction_suffix = PredictionTargetSuffix(strategy, versions.get("prediction_target"), versions.get("scoring_profile"))
    if freeze_version not in {
        coordinate_identity + prediction_suffix + (":formal" if saved.get("is_formal") is True else ""),
        coordinate_identity + ":" + freeze_hash[:16],
    }:
        raise ValueError("historical freeze saved identity invalid")
    version = _text(raw, "version")
    if version not in {
        f"record:{strategy.value}:{trade_date.isoformat()}:{stage}:{sequence}" + prediction_suffix,
        f"record:{strategy.value}:{trade_date.isoformat()}:{record_hash[:16]}",
    }:
        raise ValueError("historical record saved identity invalid")
    items = tuple(DecodeFreezeItem(item) for item in _list(saved, "items"))
    selected = sorted(item.rank for item in items if item.selected)
    if len({item.code for item in items}) != len(items) or selected != list(range(1, len(selected) + 1)):
        raise ValueError("historical freeze saved seats invalid")
    pipeline = DecodeFreezePipeline(saved.get("pipeline"))
    snapshot = HistoricalFreezeSnapshot(
        strategy, trade_date, sequence, observed_at, cast(FreezeStage, stage),
        _optional_text(saved.get("parent_version")), input_versions,
        _text(saved, "config_version"), _text(saved, "strategy_version"), items,
        _counts(saved, "filter_aggregates"), tuple(_strings(saved, "degraded_reasons")),
        _optional_count(saved.get("population_count")), _optional_count(saved.get("rejected_count")),
        DecodeSelectionDiagnostics(saved.get("selection_diagnostics")), pipeline,
        _optional_boolean(saved.get("is_formal")), freeze_version, freeze_hash,
    )
    return HistoricalFreezeRecord(snapshot, committed_at, _text(raw, "commit_kind"), record_hash, version, payload)


def _text(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("historical freeze text invalid")
    return value


def _optional_text(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("historical freeze optional text invalid")
    return value


def _time(raw: dict[str, object], name: str) -> datetime:
    value = datetime.fromisoformat(_text(raw, name))
    if value.utcoffset() != timedelta(hours=8):
        raise ValueError("historical freeze time invalid")
    return value


def _list(raw: dict[str, object], name: str) -> list[object]:
    value = raw.get(name)
    if not isinstance(value, list):
        raise ValueError("historical freeze list invalid")
    return value


def _pairs(raw: dict[str, object], name: str) -> tuple[tuple[str, str], ...]:
    pairs = _list(raw, name)
    if any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(x, str) for x in pair) for pair in pairs):
        raise ValueError("historical freeze identity pairs invalid")
    return tuple((pair[0], pair[1]) for pair in cast(list[list[str]], pairs))


def _counts(raw: dict[str, object], name: str) -> tuple[tuple[str, int], ...]:
    pairs = _list(raw, name)
    if any(not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
           or type(pair[1]) is not int or pair[1] < 0 for pair in pairs):
        raise ValueError("historical freeze count pairs invalid")
    return tuple((str(pair[0]), int(pair[1])) for pair in cast(list[list[str | int]], pairs))


def _strings(raw: dict[str, object], name: str) -> list[str]:
    values = _list(raw, name)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("historical freeze strings invalid")
    return cast(list[str], values)


def _optional_count(value: object) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("historical freeze optional count invalid")
    return value


def _optional_boolean(value: object) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError("historical freeze optional boolean invalid")
    return value

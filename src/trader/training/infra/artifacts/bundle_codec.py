"""Decode and validate one portable, hash-bound trained strategy-head bundle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT, ExposureContract
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.publication.models import Strategy
from trader.infra.artifacts.canonical import content_hash
from trader.infra.artifacts.fields import (
    is_boolean,
    is_finite_number,
    is_integer,
    is_non_empty_text,
    is_sha256_text,
)
from trader.training.infra.artifacts.contracts import TrainedProfileContract

_MODEL_FIELDS = {
    "schema_version",
    "profile_id",
    "model_id",
    "strategy_head",
    "feature_ids",
    "feature_units",
    "exposure_contract",
    "training_input_scope",
    "training_input_hash",
    "label_cutoff",
    "source_identity_hash",
    "training_input_document_hash",
    "training_input_codes",
    "training_universe_codes",
    "split_hash",
    "report_hash",
    "feature_manifest_hash",
    "training_contract_hash",
    "label_target",
    "training_cost_bps",
    "validation_scope",
    "historical_status",
    "historical_failure_reasons",
    "model_payload_hash",
    "training_anchor",
    "runtime_anchor",
    "point_in_time_parity",
    "training_rows",
    "validation_rows",
    "industry_count",
    "ensemble_weights",
    "industries",
    "dependencies",
    "automatic_model_update",
    "production_authority",
}
_REPORT_FIELDS = {
    "schema_version",
    "profile_id",
    "model_id",
    "strategy_head",
    "training_input_scope",
    "training_input_hash",
    "label_cutoff",
    "source_identity_hash",
    "training_input_document_hash",
    "training_input_codes",
    "training_universe_codes",
    "feature_manifest_hash",
    "split_hash",
    "training_contract_hash",
    "model_payload_hash",
    "label_target",
    "training_cost_bps",
    "training_anchor",
    "runtime_anchor",
    "point_in_time_parity",
    "validation_scope",
    "historical_status",
    "historical_failure_reasons",
    "industry_count",
    "training_rows",
    "validation_rows",
    "target_metrics",
    "validation_passed",
    "failure_reasons",
    "automatic_model_update",
    "production_authority",
}
_INPUT_FIELDS = {
    "schema_version",
    "profile_id",
    "strategy_head",
    "training_input_scope",
    "training_input_hash",
    "label_cutoff",
    "source_identity_hash",
    "calendar_hash",
    "source_cutoff",
    "requested_sessions",
    "input_descriptor_hash",
    "training_input_codes",
    "training_universe_codes",
    "codes",
    "feature_manifest_hash",
    "split_hash",
    "training_contract_hash",
    "label_target",
    "training_cost_bps",
    "validation_scope",
    "production_authority",
}
_INDUSTRY_FIELDS = {
    "transformer_means",
    "transformer_scales",
    "ridge_intercept",
    "ridge_coefficients",
    "lightgbm_model",
    "lightgbm_best_iteration",
    "calibration_intercept",
    "calibration_slope",
    "training_rows",
    "validation_rows",
}


@dataclass(frozen=True)
class TrainedIndustryModelArtifact:
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    ridge_intercept: float
    ridge_coefficients: tuple[float, ...]
    lightgbm_model: str
    lightgbm_best_iteration: int
    calibration_intercept: float
    calibration_slope: float
    training_rows: int
    validation_rows: int


@dataclass(frozen=True)
class TrainedHeadBundleArtifact:
    serialization_profile_id: ScoringProfileId
    strategy: Strategy
    model_id: str
    feature_ids: tuple[str, ...]
    feature_units: tuple[str, ...]
    exposure_contract: ExposureContract
    ridge_weight: float
    lightgbm_weight: float
    training_input_scope: Literal["complete_manifest"]
    training_input_hash: str
    label_cutoff: date
    source_identity_hash: str
    training_input_document_hash: str
    training_input_codes: int
    training_universe_codes: int
    split_hash: str
    report_hash: str
    feature_manifest_hash: str
    training_contract_hash: str
    model_payload_hash: str
    label_target: str
    historical_status: Literal["historical_data_insufficient"]
    historical_failure_reasons: tuple[str, ...]
    training_anchor: Literal["15:00_close_proxy"]
    runtime_anchor: Literal["14:50"]
    point_in_time_parity: Literal[False]
    training_rows: int
    validation_rows: int
    industries: tuple[tuple[str, TrainedIndustryModelArtifact], ...]
    dependencies: tuple[tuple[str, str], ...]
    content_hash: str


@dataclass(frozen=True)
class _GroupIdentity:
    content_hash: str
    strategy: Strategy
    model_id: str
    training_input_hash: str
    label_cutoff: date
    source_identity_hash: str
    training_input_document_hash: str
    feature_manifest_hash: str
    split_hash: str
    training_contract_hash: str
    model_payload_hash: str | None
    training_input_codes: int
    training_universe_codes: int


def load_head_bundle(
    path: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> TrainedHeadBundleArtifact:
    artifact = decode_head_bundle(json.loads(path.read_text(encoding="utf-8")), strategy, profile)
    report = _decode_group_document(
        json.loads(path.with_name("report.json").read_text(encoding="utf-8")), strategy, profile, report=True
    )
    training_input = _decode_group_document(
        json.loads(path.with_name("training-input.json").read_text(encoding="utf-8")), strategy, profile, report=False
    )
    expected = (
        artifact.strategy,
        artifact.model_id,
        artifact.training_input_hash,
        artifact.label_cutoff,
        artifact.source_identity_hash,
        artifact.feature_manifest_hash,
        artifact.split_hash,
        artifact.training_contract_hash,
        artifact.training_input_codes,
        artifact.training_universe_codes,
    )
    if (
        artifact.report_hash != report.content_hash
        or artifact.training_input_document_hash != training_input.content_hash
        or report.training_input_document_hash != training_input.content_hash
        or report.model_payload_hash != artifact.model_payload_hash
        or _identity_tuple(report) != expected
        or _identity_tuple(training_input) != expected
    ):
        raise ValueError("trained strategy-head bundle files have inconsistent identities")
    return artifact


def decode_head_bundle(
    document: object,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> TrainedHeadBundleArtifact:
    payload, declared_hash = _hashed_object(document, _MODEL_FIELDS, "model")
    contract = profile.head_for_strategy(strategy)
    feature_ids = tuple(_string_list(payload, "feature_ids"))
    feature_units = tuple(_string_list(payload, "feature_units"))
    industries = _decode_industries(payload, len(feature_ids))
    dependencies = _dependencies(payload)
    weights = payload.get("ensemble_weights")
    if not isinstance(weights, dict) or set(weights) != {"ridge", "lightgbm"}:
        raise ValueError("trained-head ensemble weights are invalid")
    ridge_weight = _number(weights, "ridge")
    lightgbm_weight = _number(weights, "lightgbm")
    if (
        _text(payload, "schema_version") != f"{profile.profile_id}_head_scoring_model"
        or _text(payload, "profile_id") != profile.profile_id
        or _text(payload, "strategy_head") != strategy.value
        or _text(payload, "model_id") != contract.model_id
        or feature_ids != contract.feature_manifest.names
        or feature_units != contract.feature_manifest.units
        or _text(payload, "feature_manifest_hash") != contract.feature_manifest.content_hash
        or _text(payload, "label_target") != contract.label_target
        or _text(payload, "training_anchor") != "15:00_close_proxy"
        or _text(payload, "runtime_anchor") != contract.runtime_anchor
        or _text(payload, "validation_scope") != "daily_close_engineering_proxy"
        or _text(payload, "historical_status") != "historical_data_insufficient"
        or tuple(_string_list(payload, "historical_failure_reasons")) != ("daily_close_proxy_not_point_in_time",)
        or _integer(payload, "training_cost_bps") != 0
        or _boolean(payload, "point_in_time_parity")
        or _boolean(payload, "automatic_model_update")
        or _boolean(payload, "production_authority")
        or (ridge_weight, lightgbm_weight) != (0.5, 0.5)
        or _exposure_contract(payload) != TRAINED_HEAD_EXPOSURE_CONTRACT
        or len(industries) != _integer(payload, "industry_count")
        or not industries
        or _integer(payload, "training_rows") < 1
        or _integer(payload, "validation_rows") < 1
        or _model_payload_hash(payload) != _text(payload, "model_payload_hash")
    ):
        raise ValueError("trained strategy-head model contract is invalid")
    for name in (
        "training_input_hash",
        "source_identity_hash",
        "training_input_document_hash",
        "split_hash",
        "report_hash",
        "feature_manifest_hash",
        "training_contract_hash",
        "model_payload_hash",
    ):
        _require_sha256(payload, name)
    input_codes = _integer(payload, "training_input_codes")
    universe_codes = _integer(payload, "training_universe_codes")
    if input_codes < 1 or universe_codes != input_codes:
        raise ValueError("trained-head complete training input coverage is invalid")
    return TrainedHeadBundleArtifact(
        profile.profile_id,
        strategy,
        contract.model_id,
        feature_ids,
        feature_units,
        TRAINED_HEAD_EXPOSURE_CONTRACT,
        ridge_weight,
        lightgbm_weight,
        "complete_manifest",
        _text(payload, "training_input_hash"),
        _date(payload, "label_cutoff"),
        _text(payload, "source_identity_hash"),
        _text(payload, "training_input_document_hash"),
        input_codes,
        universe_codes,
        _text(payload, "split_hash"),
        _text(payload, "report_hash"),
        _text(payload, "feature_manifest_hash"),
        _text(payload, "training_contract_hash"),
        _text(payload, "model_payload_hash"),
        contract.label_target,
        "historical_data_insufficient",
        ("daily_close_proxy_not_point_in_time",),
        "15:00_close_proxy",
        contract.runtime_anchor,
        False,
        _integer(payload, "training_rows"),
        _integer(payload, "validation_rows"),
        industries,
        dependencies,
        declared_hash,
    )


def _decode_group_document(
    document: object,
    strategy: Strategy,
    profile: TrainedProfileContract,
    *,
    report: bool,
) -> _GroupIdentity:
    fields = _REPORT_FIELDS if report else _INPUT_FIELDS
    payload, declared_hash = _hashed_object(document, fields, "report" if report else "training input")
    contract = profile.head_for_strategy(strategy)
    expected_schema = (
        f"{profile.profile_id}_head_training_report" if report else f"{profile.profile_id}_head_training_input"
    )
    if (
        _text(payload, "schema_version") != expected_schema
        or _text(payload, "profile_id") != profile.profile_id
        or _text(payload, "strategy_head") != strategy.value
        or _text(payload, "training_input_scope") != "complete_manifest"
        or _text(payload, "feature_manifest_hash") != contract.feature_manifest.content_hash
        or _text(payload, "label_target") != contract.label_target
        or _integer(payload, "training_cost_bps") != 0
        or _text(payload, "validation_scope") != "daily_close_engineering_proxy"
        or _boolean(payload, "production_authority")
    ):
        raise ValueError("trained strategy-head group document contract is invalid")
    if report and (
        _text(payload, "model_id") != contract.model_id
        or _text(payload, "training_anchor") != "15:00_close_proxy"
        or _text(payload, "runtime_anchor") != contract.runtime_anchor
        or _boolean(payload, "point_in_time_parity")
        or _text(payload, "historical_status") != "historical_data_insufficient"
        or tuple(_string_list(payload, "historical_failure_reasons")) != ("daily_close_proxy_not_point_in_time",)
        or not _boolean(payload, "validation_passed")
        or _string_list(payload, "failure_reasons")
        or _boolean(payload, "automatic_model_update")
        or not _valid_target_metrics(payload.get("target_metrics"), strategy)
    ):
        raise ValueError("trained strategy-head report is invalid")
    for name in (
        "training_input_hash",
        "source_identity_hash",
        "feature_manifest_hash",
        "split_hash",
        "training_contract_hash",
    ):
        _require_sha256(payload, name)
    model_payload_hash = _text(payload, "model_payload_hash") if report else None
    training_document_hash = _text(payload, "training_input_document_hash") if report else declared_hash
    if report:
        _require_sha256(payload, "training_input_document_hash")
        _require_sha256(payload, "model_payload_hash")
    input_codes = _integer(payload, "training_input_codes")
    universe_codes = _integer(payload, "training_universe_codes")
    if input_codes < 1 or input_codes != universe_codes:
        raise ValueError("trained strategy-head input coverage is invalid")
    if not report:
        _validate_training_input(payload, input_codes)
    return _GroupIdentity(
        declared_hash,
        strategy,
        contract.model_id,
        _text(payload, "training_input_hash"),
        _date(payload, "label_cutoff"),
        _text(payload, "source_identity_hash"),
        training_document_hash,
        _text(payload, "feature_manifest_hash"),
        _text(payload, "split_hash"),
        _text(payload, "training_contract_hash"),
        model_payload_hash,
        input_codes,
        universe_codes,
    )


def _validate_training_input(payload: dict[str, object], input_codes: int) -> None:
    for name in ("calendar_hash", "input_descriptor_hash"):
        _require_sha256(payload, name)
    source_cutoff = _date(payload, "source_cutoff")
    if _date(payload, "label_cutoff") >= source_cutoff:
        raise ValueError("trained-head input label cutoff is invalid")
    requested_sessions = _integer(payload, "requested_sessions")
    codes = _string_list(payload, "codes")
    if (
        not 1_250 <= requested_sessions <= 2_000
        or codes != sorted(set(codes))
        or len(codes) != input_codes
        or any(len(code) != 6 or not code.isdigit() for code in codes)
    ):
        raise ValueError("trained-head input manifest is invalid")


def _identity_tuple(value: _GroupIdentity) -> tuple[object, ...]:
    return (
        value.strategy,
        value.model_id,
        value.training_input_hash,
        value.label_cutoff,
        value.source_identity_hash,
        value.feature_manifest_hash,
        value.split_hash,
        value.training_contract_hash,
        value.training_input_codes,
        value.training_universe_codes,
    )


def _hashed_object(document: object, fields: set[str], label: str) -> tuple[dict[str, object], str]:
    if not isinstance(document, dict):
        raise TypeError(f"trained-head {label} must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    declared_hash = payload.pop("content_hash", None)
    if not isinstance(declared_hash, str) or content_hash(payload) != declared_hash or set(payload) != fields:
        raise ValueError(f"trained-head {label} fields or content hash are invalid")
    return payload, declared_hash


def _decode_industries(payload: dict[str, object], width: int) -> tuple[tuple[str, TrainedIndustryModelArtifact], ...]:
    raw = payload.get("industries")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("trained-head industry models are missing")
    result: list[tuple[str, TrainedIndustryModelArtifact]] = []
    for industry, document in raw.items():
        if not isinstance(industry, str) or not industry.strip() or not isinstance(document, dict):
            raise TypeError("trained-head industry model identity is invalid")
        values = cast(dict[str, object], document)
        if set(values) != _INDUSTRY_FIELDS:
            raise ValueError("trained-head industry model fields are invalid")
        means = tuple(_number_list(values, "transformer_means"))
        scales = tuple(_number_list(values, "transformer_scales"))
        coefficients = tuple(_number_list(values, "ridge_coefficients"))
        if (
            len(means) != width
            or len(scales) != width
            or len(coefficients) != width
            or any(value <= 0.0 for value in scales)
            or _integer(values, "lightgbm_best_iteration") < 1
            or _integer(values, "training_rows") < 1
            or _integer(values, "validation_rows") < 1
        ):
            raise ValueError("trained-head industry model dimensions are invalid")
        result.append(
            (
                industry.strip(),
                TrainedIndustryModelArtifact(
                    means,
                    scales,
                    _number(values, "ridge_intercept"),
                    coefficients,
                    _text(values, "lightgbm_model"),
                    _integer(values, "lightgbm_best_iteration"),
                    _number(values, "calibration_intercept"),
                    _number(values, "calibration_slope"),
                    _integer(values, "training_rows"),
                    _integer(values, "validation_rows"),
                ),
            )
        )
    return tuple(sorted(result))


def _exposure_contract(payload: dict[str, object]) -> ExposureContract:
    raw = payload.get("exposure_contract")
    if not isinstance(raw, dict) or set(raw) != {"market", "board", "industry", "log_average_amount_20d", "order"}:
        raise ValueError("trained-head exposure contract is invalid")
    if any(raw.get(name) is not True for name in ("market", "board", "industry", "log_average_amount_20d")):
        raise ValueError("trained-head exposure contract is invalid")
    if tuple(_string_list(raw, "order")) != TRAINED_HEAD_EXPOSURE_CONTRACT.order:
        raise ValueError("trained-head exposure order is invalid")
    return TRAINED_HEAD_EXPOSURE_CONTRACT


def _dependencies(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
    raw = payload.get("dependencies")
    if not isinstance(raw, dict) or set(raw) != {"lightgbm", "numpy"}:
        raise ValueError("trained-head dependency identity is invalid")
    values = tuple(sorted((str(name), str(value)) for name, value in raw.items()))
    if any(not value for _, value in values):
        raise ValueError("trained-head dependency version is invalid")
    return values


def _valid_target_metrics(value: object, strategy: Strategy) -> bool:
    expected = (
        {"target_t2", "target_t3", "target_t4", "target_t5", "target_d25_aggregate"}
        if strategy is Strategy.D25
        else {"target_t1"}
    )
    if not isinstance(value, dict) or set(value) != expected:
        return False
    for raw in value.values():
        if not isinstance(raw, dict) or set(raw) != {"count", "mean", "standard_deviation"}:
            return False
        count = raw.get("count")
        mean = raw.get("mean")
        deviation = raw.get("standard_deviation")
        if (
            not is_integer(count)
            or count < 1
            or not is_finite_number(mean)
            or not is_finite_number(deviation)
            or deviation < 0.0
        ):
            return False
    return True


def _model_payload_hash(payload: dict[str, object]) -> str:
    return content_hash(
        {key: value for key, value in payload.items() if key not in {"report_hash", "model_payload_hash"}}
    )


def _require_sha256(payload: dict[str, object], name: str) -> None:
    value = _text(payload, name)
    if not is_sha256_text(value):
        raise ValueError(f"trained-head {name} is not a SHA-256 value")


def _date(payload: dict[str, object], name: str) -> date:
    try:
        return date.fromisoformat(_text(payload, name))
    except ValueError as exc:
        raise ValueError(f"trained-head {name} is invalid") from exc


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not is_non_empty_text(value):
        raise TypeError(f"trained-head {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not is_integer(value):
        raise TypeError(f"trained-head {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if not is_finite_number(value):
        raise TypeError(f"trained-head {name} must be finite numeric")
    return float(value)


def _boolean(payload: dict[str, object], name: str) -> bool:
    value = payload.get(name)
    if not is_boolean(value):
        raise TypeError(f"trained-head {name} must be boolean")
    return value


def _string_list(payload: dict[str, object], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"trained-head {name} must be a string list")
    return cast(list[str], value)


def _number_list(payload: dict[str, object], name: str) -> list[float]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise TypeError(f"trained-head {name} must be a numeric list")
    return [_number({name: item}, name) for item in value]


__all__ = ["TrainedHeadBundleArtifact", "TrainedIndustryModelArtifact", "decode_head_bundle", "load_head_bundle"]

"""Decode and validate one portable, hash-bound trained strategy-head bundle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.fields import (
    IsBoolean,
    IsFiniteNumber,
    IsInteger,
    IsNonEmptyText,
    IsSha256Text,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.residualization import TRAINED_HEAD_EXPOSURE_CONTRACT, ExposureContract
from trader.training.infra.model_bundles.contracts import TrainedAlgorithm, TrainedProfileContract

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
    "algorithm",
    "industries",
    "dependencies",
    "automatic_model_update",
    "production_authority",
}
_REPORT_COMMON_FIELDS = {
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
    "automatic_model_update",
    "production_authority",
}
_REPORT_FIELDS = {
    *_REPORT_COMMON_FIELDS,
    "artifact_structure_valid",
    "artifact_structure_failure_reasons",
    "prediction_return_validation_passed",
    "prediction_return_failure_reasons",
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
_INDUSTRY_COMMON_FIELDS = {
    "transformer_means",
    "transformer_scales",
    "calibration_intercept",
    "calibration_slope",
    "training_rows",
    "validation_rows",
}


@dataclass(frozen=True)
class TrainedIndustryModelSnapshot:
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    calibration_intercept: float
    calibration_slope: float
    training_rows: int
    validation_rows: int


@dataclass(frozen=True)
class TrainedRidgeModelSnapshot(TrainedIndustryModelSnapshot):
    ridge_intercept: float
    ridge_coefficients: tuple[float, ...]


@dataclass(frozen=True)
class TrainedLightGBMModelSnapshot(TrainedIndustryModelSnapshot):
    lightgbm_model: str
    lightgbm_best_iteration: int


@dataclass(frozen=True)
class TrainedHeadModelBundle:
    serialization_profile_id: ScoringProfileId
    strategy: Strategy
    model_id: str
    feature_ids: tuple[str, ...]
    feature_units: tuple[str, ...]
    exposure_contract: ExposureContract
    algorithm: TrainedAlgorithm
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
    runtime_anchor: Literal["15:00"]
    point_in_time_parity: Literal[False]
    training_rows: int
    validation_rows: int
    industries: tuple[tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot], ...]
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


def LoadHeadBundle(
    path: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> TrainedHeadModelBundle:
    bundle = DecodeHeadBundle(json.loads(path.read_text(encoding="utf-8")), strategy, profile)
    report = _decode_group_document(
        json.loads(path.with_name("report.json").read_text(encoding="utf-8")), strategy, profile, report=True
    )
    training_input = _decode_group_document(
        json.loads(path.with_name("training-input.json").read_text(encoding="utf-8")), strategy, profile, report=False
    )
    expected = (
        bundle.strategy,
        bundle.model_id,
        bundle.training_input_hash,
        bundle.label_cutoff,
        bundle.source_identity_hash,
        bundle.feature_manifest_hash,
        bundle.split_hash,
        bundle.training_contract_hash,
        bundle.training_input_codes,
        bundle.training_universe_codes,
    )
    if (
        bundle.report_hash != report.content_hash
        or bundle.training_input_document_hash != training_input.content_hash
        or report.training_input_document_hash != training_input.content_hash
        or report.model_payload_hash != bundle.model_payload_hash
        or _identity_tuple(report) != expected
        or _identity_tuple(training_input) != expected
    ):
        raise ValueError("trained strategy-head bundle files have inconsistent identities")
    return bundle


def DecodeHeadBundle(
    document: object,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> TrainedHeadModelBundle:
    payload, declared_hash = _hashed_object(document, _MODEL_FIELDS, "model")
    contract = profile.head_for_strategy(strategy)
    feature_ids = tuple(_string_list(payload, "feature_ids"))
    feature_units = tuple(_string_list(payload, "feature_units"))
    industries = _decode_industries(payload, len(feature_ids), profile.algorithm)
    dependencies = _dependencies(payload, profile.algorithm)
    if (
        _text(payload, "schema_version") != f"{profile.profile_id}_head_scoring_model"
        or _text(payload, "profile_id") != profile.profile_id
        or _text(payload, "strategy_head") != strategy.value
        or _text(payload, "model_id") != contract.model_id
        or _text(payload, "algorithm") != profile.algorithm
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
    return TrainedHeadModelBundle(
        profile.profile_id,
        strategy,
        contract.model_id,
        feature_ids,
        feature_units,
        TRAINED_HEAD_EXPOSURE_CONTRACT,
        profile.algorithm,
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
        f"{profile.profile_id}_head_training_report_validation_split"
        if report
        else f"{profile.profile_id}_head_training_input"
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
        or _boolean(payload, "automatic_model_update")
        or not _valid_target_metrics(payload.get("target_metrics"), strategy)
    ):
        raise ValueError("trained strategy-head report is invalid")
    if report and (
        not _boolean(payload, "artifact_structure_valid")
        or _string_list(payload, "artifact_structure_failure_reasons")
        or _boolean(payload, "prediction_return_validation_passed")
        or tuple(_string_list(payload, "prediction_return_failure_reasons")) != ("daily_close_proxy_not_point_in_time",)
    ):
        raise ValueError("trained strategy-head validation semantics are invalid")
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
    if not isinstance(declared_hash, str) or ContentHash(payload) != declared_hash or set(payload) != fields:
        raise ValueError(f"trained-head {label} fields or content hash are invalid")
    return payload, declared_hash


def _decode_industries(
    payload: dict[str, object], width: int, algorithm: TrainedAlgorithm
) -> tuple[tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot], ...]:
    raw = payload.get("industries")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("trained-head industry models are missing")
    result: list[tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot]] = []
    model_fields = (
        {"lightgbm_model", "lightgbm_best_iteration"}
        if algorithm == "lightgbm"
        else {"ridge_intercept", "ridge_coefficients"}
    )
    for industry, document in raw.items():
        if not isinstance(industry, str) or not industry.strip() or not isinstance(document, dict):
            raise TypeError("trained-head industry model identity is invalid")
        values = cast(dict[str, object], document)
        if set(values) != _INDUSTRY_COMMON_FIELDS | model_fields:
            raise ValueError("trained-head industry model fields are invalid")
        means = tuple(_number_list(values, "transformer_means"))
        scales = tuple(_number_list(values, "transformer_scales"))
        if (
            len(means) != width
            or len(scales) != width
            or any(value <= 0.0 for value in scales)
            or _integer(values, "training_rows") < 1
            or _integer(values, "validation_rows") < 1
        ):
            raise ValueError("trained-head industry model dimensions are invalid")
        calibration_intercept = _number(values, "calibration_intercept")
        calibration_slope = _number(values, "calibration_slope")
        training_rows = _integer(values, "training_rows")
        validation_rows = _integer(values, "validation_rows")
        if algorithm == "lightgbm":
            if _integer(values, "lightgbm_best_iteration") < 1:
                raise ValueError("trained-head LightGBM iteration is invalid")
            model: TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot = TrainedLightGBMModelSnapshot(
                transformer_means=means,
                transformer_scales=scales,
                calibration_intercept=calibration_intercept,
                calibration_slope=calibration_slope,
                training_rows=training_rows,
                validation_rows=validation_rows,
                lightgbm_model=_text(values, "lightgbm_model"),
                lightgbm_best_iteration=_integer(values, "lightgbm_best_iteration"),
            )
        else:
            coefficients = tuple(_number_list(values, "ridge_coefficients"))
            if len(coefficients) != width:
                raise ValueError("trained-head Ridge dimensions are invalid")
            model = TrainedRidgeModelSnapshot(
                transformer_means=means,
                transformer_scales=scales,
                calibration_intercept=calibration_intercept,
                calibration_slope=calibration_slope,
                training_rows=training_rows,
                validation_rows=validation_rows,
                ridge_intercept=_number(values, "ridge_intercept"),
                ridge_coefficients=coefficients,
            )
        result.append(
            (
                industry.strip(),
                model,
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


def _dependencies(payload: dict[str, object], algorithm: TrainedAlgorithm) -> tuple[tuple[str, str], ...]:
    raw = payload.get("dependencies")
    expected = {"numpy", "lightgbm"} if algorithm == "lightgbm" else {"numpy"}
    if not isinstance(raw, dict) or set(raw) != expected:
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
            not IsInteger(count)
            or count < 1
            or not IsFiniteNumber(mean)
            or not IsFiniteNumber(deviation)
            or deviation < 0.0
        ):
            return False
    return True


def _model_payload_hash(payload: dict[str, object]) -> str:
    return ContentHash(
        {key: value for key, value in payload.items() if key not in {"report_hash", "model_payload_hash"}}
    )


def _require_sha256(payload: dict[str, object], name: str) -> None:
    value = _text(payload, name)
    if not IsSha256Text(value):
        raise ValueError(f"trained-head {name} is not a SHA-256 value")


def _date(payload: dict[str, object], name: str) -> date:
    try:
        return date.fromisoformat(_text(payload, name))
    except ValueError as exc:
        raise ValueError(f"trained-head {name} is invalid") from exc


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not IsNonEmptyText(value):
        raise TypeError(f"trained-head {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not IsInteger(value):
        raise TypeError(f"trained-head {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if not IsFiniteNumber(value):
        raise TypeError(f"trained-head {name} must be finite numeric")
    return float(value)


def _boolean(payload: dict[str, object], name: str) -> bool:
    value = payload.get(name)
    if not IsBoolean(value):
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


__all__ = [
    "TrainedHeadModelBundle",
    "TrainedIndustryModelSnapshot",
    "TrainedRidgeModelSnapshot",
    "TrainedLightGBMModelSnapshot",
    "DecodeHeadBundle",
    "LoadHeadBundle",
]

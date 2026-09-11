"""Decode and validate a hash-bound V3 Tomorrow training bundle."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.domain.recommendation.model_scoring import V3_EXPOSURE_CONTRACT, ExposureContract
from trader.infra.scoring.artifact_hashing import artifact_content_hash

_FEATURE_IDS = TOMORROW_MODEL_FEATURE_MANIFEST.names
_FEATURE_UNITS = TOMORROW_MODEL_FEATURE_MANIFEST.units
_FEATURE_MANIFEST_HASH = TOMORROW_MODEL_FEATURE_MANIFEST.content_hash
_MODEL_ID = "industry_ridge_lightgbm"
_DOCUMENT_FIELDS = {
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
    "source_commit",
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
    "model_id",
    "training_input_scope",
    "training_input_hash",
    "label_cutoff",
    "source_identity_hash",
    "training_input_document_hash",
    "training_input_codes",
    "training_universe_codes",
    "feature_manifest_hash",
    "split_hash",
    "source_commit",
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
    "validation_passed",
    "failure_reasons",
    "automatic_model_update",
    "production_authority",
}
_TRAINING_INPUT_FIELDS = {
    "schema_version",
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
    "source_commit",
    "feature_manifest_hash",
    "training_contract_hash",
    "label_target",
    "training_cost_bps",
    "validation_scope",
    "production_authority",
}
_INDUSTRY_MODEL_FIELDS = {
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
class V3IndustryModelArtifact:
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
class V3TomorrowBundleArtifact:
    profile_id: Literal["v3"]
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
    source_commit: str
    feature_manifest_hash: str
    training_contract_hash: str
    model_payload_hash: str
    label_target: Literal["pre_cost_excess_return"]
    training_cost_bps: Literal[0]
    validation_scope: Literal["daily_close_engineering_proxy"]
    historical_status: Literal["historical_data_insufficient"]
    historical_failure_reasons: tuple[str, ...]
    training_anchor: Literal["15:00_close_proxy"]
    runtime_anchor: Literal["14:50"]
    point_in_time_parity: Literal[False]
    training_rows: int
    validation_rows: int
    industries: tuple[tuple[str, V3IndustryModelArtifact], ...]
    dependencies: tuple[tuple[str, str], ...]
    content_hash: str


@dataclass(frozen=True)
class _DecodedContract:
    stored_hash: str
    feature_ids: tuple[str, ...]
    feature_units: tuple[str, ...]
    exposure_contract: ExposureContract
    ridge_weight: float
    lightgbm_weight: float


@dataclass(frozen=True)
class _V3TrainingReportArtifact:
    content_hash: str
    training_input_scope: Literal["complete_manifest"]
    training_input_hash: str
    label_cutoff: date
    source_identity_hash: str
    training_input_document_hash: str
    training_input_codes: int
    training_universe_codes: int
    feature_manifest_hash: str
    split_hash: str
    source_commit: str
    training_contract_hash: str
    model_payload_hash: str
    historical_failure_reasons: tuple[str, ...]
    industry_count: int
    training_rows: int
    validation_rows: int


@dataclass(frozen=True)
class _V3TrainingInputArtifact:
    content_hash: str
    training_input_scope: Literal["complete_manifest"]
    training_input_hash: str
    label_cutoff: date
    source_identity_hash: str
    feature_manifest_hash: str
    source_commit: str
    training_contract_hash: str
    training_input_codes: int
    training_universe_codes: int


def load_tomorrow_bundle(path: Path) -> V3TomorrowBundleArtifact:
    document = json.loads(path.read_text(encoding="utf-8"))
    artifact = decode_tomorrow_bundle(document)
    report_path = path.with_name("report.json")
    report = _decode_training_report(json.loads(report_path.read_text(encoding="utf-8")))
    input_path = path.with_name("training-input.json")
    training_input = _decode_training_input(json.loads(input_path.read_text(encoding="utf-8")))
    _validate_bundle_group(artifact, report, training_input)
    return artifact


def decode_tomorrow_bundle(document: object) -> V3TomorrowBundleArtifact:
    if not isinstance(document, dict):
        raise TypeError("Tomorrow V3 training model must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    contract = _decode_contract(payload)
    dependencies = _dependencies(payload)
    industries = _decode_industries(payload, len(contract.feature_ids))
    if len(industries) != _integer(payload, "industry_count"):
        raise ValueError("Tomorrow V3 industry model count is invalid")
    return V3TomorrowBundleArtifact(
        "v3",
        _MODEL_ID,
        contract.feature_ids,
        contract.feature_units,
        contract.exposure_contract,
        contract.ridge_weight,
        contract.lightgbm_weight,
        _training_input_scope(payload),
        _text(payload, "training_input_hash"),
        _date(payload, "label_cutoff"),
        _text(payload, "source_identity_hash"),
        _text(payload, "training_input_document_hash"),
        _integer(payload, "training_input_codes"),
        _integer(payload, "training_universe_codes"),
        _text(payload, "split_hash"),
        _text(payload, "report_hash"),
        _text(payload, "source_commit"),
        _text(payload, "feature_manifest_hash"),
        _text(payload, "training_contract_hash"),
        _text(payload, "model_payload_hash"),
        "pre_cost_excess_return",
        0,
        "daily_close_engineering_proxy",
        "historical_data_insufficient",
        tuple(_string_list(payload, "historical_failure_reasons")),
        "15:00_close_proxy",
        "14:50",
        False,
        _integer(payload, "training_rows"),
        _integer(payload, "validation_rows"),
        industries,
        dependencies,
        contract.stored_hash,
    )


def _decode_contract(
    payload: dict[str, object],
) -> _DecodedContract:
    stored_hash = payload.pop("content_hash", None)
    if not isinstance(stored_hash, str) or artifact_content_hash(payload) != stored_hash:
        raise ValueError("Tomorrow V3 training model content hash is invalid")
    missing_fields = _DOCUMENT_FIELDS - set(payload)
    if missing_fields & {"feature_ids", "feature_units"}:
        raise ValueError("Tomorrow V3 feature contract is incomplete")
    if "exposure_contract" in missing_fields:
        raise ValueError("Tomorrow V3 exposure contract is incomplete")
    if "ensemble_weights" in missing_fields:
        raise ValueError("Tomorrow V3 ensemble weights are incomplete")
    if set(payload) != _DOCUMENT_FIELDS:
        raise ValueError("Tomorrow V3 training model fields are invalid")
    feature_ids = tuple(_string_list(payload, "feature_ids"))
    feature_units = tuple(_string_list(payload, "feature_units"))
    exposure_contract = _exposure_contract(payload)
    ridge_weight, lightgbm_weight = _ensemble_weights(payload)
    if (
        _text(payload, "schema_version") != "tomorrow_scoring_model"
        or _text(payload, "profile_id") != "v3"
        or _text(payload, "model_id") != _MODEL_ID
        or _text(payload, "strategy_head") != "tomorrow"
        or feature_ids != _FEATURE_IDS
        or feature_units != _FEATURE_UNITS
        or exposure_contract != V3_EXPOSURE_CONTRACT
        or not _sha256_text(payload, "training_input_hash")
        or not _sha256_text(payload, "source_identity_hash")
        or not _sha256_text(payload, "training_input_document_hash")
        or not _sha256_text(payload, "split_hash")
        or not _sha256_text(payload, "report_hash")
        or not _sha256_text(payload, "feature_manifest_hash")
        or _text(payload, "feature_manifest_hash") != _FEATURE_MANIFEST_HASH
        or not _sha256_text(payload, "training_contract_hash")
        or not _sha256_text(payload, "model_payload_hash")
        or not _source_commit(payload)
        or _text(payload, "label_target") != "pre_cost_excess_return"
        or _integer(payload, "training_cost_bps") != 0
        or _text(payload, "validation_scope") != "daily_close_engineering_proxy"
        or _text(payload, "historical_status") != "historical_data_insufficient"
        or not _proxy_failure_reasons(payload)
        or _integer(payload, "industry_count") < 1
        or _text(payload, "training_anchor") != "15:00_close_proxy"
        or _text(payload, "runtime_anchor") != "14:50"
        or _boolean(payload, "point_in_time_parity")
        or _boolean(payload, "automatic_model_update")
        or _boolean(payload, "production_authority")
        or _integer(payload, "training_rows") < 1
        or _integer(payload, "validation_rows") < 1
        or _model_payload_hash(payload) != _text(payload, "model_payload_hash")
    ):
        raise ValueError("Tomorrow V3 training model identity or feature contract is invalid")
    input_scope = _training_input_scope(payload)
    input_codes = _integer(payload, "training_input_codes")
    universe_codes = _integer(payload, "training_universe_codes")
    if input_codes < 1 or universe_codes < input_codes:
        raise ValueError("Tomorrow V3 training input coverage is invalid")
    if input_scope == "complete_manifest" and input_codes != universe_codes:
        raise ValueError("Tomorrow V3 complete training input coverage is invalid")
    return _DecodedContract(
        stored_hash,
        feature_ids,
        feature_units,
        exposure_contract,
        ridge_weight,
        lightgbm_weight,
    )


def _decode_training_report(document: object) -> _V3TrainingReportArtifact:
    if not isinstance(document, dict):
        raise TypeError("Tomorrow V3 training report must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    stored_hash = payload.pop("content_hash", None)
    if not isinstance(stored_hash, str) or artifact_content_hash(payload) != stored_hash:
        raise ValueError("Tomorrow V3 training report content hash is invalid")
    if set(payload) != _REPORT_FIELDS:
        raise ValueError("Tomorrow V3 training report fields are invalid")
    if (
        _text(payload, "schema_version") != "tomorrow_training_report"
        or _text(payload, "model_id") != _MODEL_ID
        or not _sha256_text(payload, "training_input_hash")
        or not _sha256_text(payload, "source_identity_hash")
        or not _sha256_text(payload, "training_input_document_hash")
        or _text(payload, "feature_manifest_hash") != _FEATURE_MANIFEST_HASH
        or not _sha256_text(payload, "split_hash")
        or not _source_commit(payload)
        or not _sha256_text(payload, "training_contract_hash")
        or not _sha256_text(payload, "model_payload_hash")
        or _text(payload, "training_anchor") != "15:00_close_proxy"
        or _text(payload, "runtime_anchor") != "14:50"
        or _boolean(payload, "point_in_time_parity")
        or _text(payload, "validation_scope") != "daily_close_engineering_proxy"
        or _text(payload, "historical_status") != "historical_data_insufficient"
        or not _proxy_failure_reasons(payload)
        or _text(payload, "label_target") != "pre_cost_excess_return"
        or _integer(payload, "training_cost_bps") != 0
        or _integer(payload, "training_input_codes") < 1
        or _integer(payload, "training_universe_codes") < _integer(payload, "training_input_codes")
        or _integer(payload, "industry_count") < 1
        or _integer(payload, "training_rows") < 1
        or _integer(payload, "validation_rows") < 1
        or not _boolean(payload, "validation_passed")
        or _string_list(payload, "failure_reasons")
        or _boolean(payload, "automatic_model_update")
        or _boolean(payload, "production_authority")
    ):
        raise ValueError("Tomorrow V3 training report identity is invalid")
    return _V3TrainingReportArtifact(
        stored_hash,
        _training_input_scope(payload),
        _text(payload, "training_input_hash"),
        _date(payload, "label_cutoff"),
        _text(payload, "source_identity_hash"),
        _text(payload, "training_input_document_hash"),
        _integer(payload, "training_input_codes"),
        _integer(payload, "training_universe_codes"),
        _text(payload, "feature_manifest_hash"),
        _text(payload, "split_hash"),
        _text(payload, "source_commit"),
        _text(payload, "training_contract_hash"),
        _text(payload, "model_payload_hash"),
        tuple(_string_list(payload, "historical_failure_reasons")),
        _integer(payload, "industry_count"),
        _integer(payload, "training_rows"),
        _integer(payload, "validation_rows"),
    )


def _decode_training_input(document: object) -> _V3TrainingInputArtifact:
    if not isinstance(document, dict):
        raise TypeError("Tomorrow V3 training input must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    stored_hash = payload.pop("content_hash", None)
    codes = _string_list(payload, "codes")
    if (
        not isinstance(stored_hash, str)
        or artifact_content_hash(payload) != stored_hash
        or set(payload) != _TRAINING_INPUT_FIELDS
        or _text(payload, "schema_version") != "tomorrow_training_input"
        or _training_input_scope(payload) != "complete_manifest"
        or not _sha256_text(payload, "training_input_hash")
        or not _sha256_text(payload, "source_identity_hash")
        or not _sha256_text(payload, "calendar_hash")
        or not _sha256_text(payload, "input_descriptor_hash")
        or _text(payload, "feature_manifest_hash") != _FEATURE_MANIFEST_HASH
        or not _source_commit(payload)
        or not _sha256_text(payload, "training_contract_hash")
        or _text(payload, "label_target") != "pre_cost_excess_return"
        or _integer(payload, "training_cost_bps") != 0
        or _text(payload, "validation_scope") != "daily_close_engineering_proxy"
        or _boolean(payload, "production_authority")
        or _integer(payload, "requested_sessions") != 2000
        or codes != sorted(set(codes))
        or any(len(code) != 6 or not code.isdigit() for code in codes)
        or len(codes) != _integer(payload, "training_input_codes")
        or len(codes) != _integer(payload, "training_universe_codes")
    ):
        raise ValueError("Tomorrow V3 training input identity is invalid")
    source_cutoff = _date(payload, "source_cutoff")
    label_cutoff = _date(payload, "label_cutoff")
    if label_cutoff >= source_cutoff:
        raise ValueError("Tomorrow V3 training input label cutoff is invalid")
    return _V3TrainingInputArtifact(
        stored_hash,
        "complete_manifest",
        _text(payload, "training_input_hash"),
        label_cutoff,
        _text(payload, "source_identity_hash"),
        _text(payload, "feature_manifest_hash"),
        _text(payload, "source_commit"),
        _text(payload, "training_contract_hash"),
        _integer(payload, "training_input_codes"),
        _integer(payload, "training_universe_codes"),
    )


def _validate_bundle_group(
    artifact: V3TomorrowBundleArtifact,
    report: _V3TrainingReportArtifact,
    training_input: _V3TrainingInputArtifact,
) -> None:
    model_values = (
        artifact.training_input_scope,
        artifact.training_input_hash,
        artifact.label_cutoff,
        artifact.source_identity_hash,
        artifact.training_input_document_hash,
        artifact.training_input_codes,
        artifact.training_universe_codes,
        artifact.feature_manifest_hash,
        artifact.split_hash,
        artifact.source_commit,
        artifact.training_contract_hash,
        artifact.model_payload_hash,
        artifact.historical_failure_reasons,
        len(artifact.industries),
        artifact.training_rows,
        artifact.validation_rows,
    )
    report_values = (
        report.training_input_scope,
        report.training_input_hash,
        report.label_cutoff,
        report.source_identity_hash,
        report.training_input_document_hash,
        report.training_input_codes,
        report.training_universe_codes,
        report.feature_manifest_hash,
        report.split_hash,
        report.source_commit,
        report.training_contract_hash,
        report.model_payload_hash,
        report.historical_failure_reasons,
        report.industry_count,
        report.training_rows,
        report.validation_rows,
    )
    input_values = (
        training_input.training_input_scope,
        training_input.training_input_hash,
        training_input.label_cutoff,
        training_input.source_identity_hash,
        training_input.feature_manifest_hash,
        training_input.source_commit,
        training_input.training_contract_hash,
        training_input.training_input_codes,
        training_input.training_universe_codes,
    )
    group_values = (
        artifact.training_input_scope,
        artifact.training_input_hash,
        artifact.label_cutoff,
        artifact.source_identity_hash,
        artifact.feature_manifest_hash,
        artifact.source_commit,
        artifact.training_contract_hash,
        artifact.training_input_codes,
        artifact.training_universe_codes,
    )
    if (
        artifact.report_hash != report.content_hash
        or model_values != report_values
        or artifact.training_input_document_hash != training_input.content_hash
        or input_values != group_values
    ):
        raise ValueError("Tomorrow V3 model, report, and training input group is inconsistent")


def _model_payload_hash(payload: dict[str, object]) -> str:
    excluded = {"content_hash", "report_hash", "model_payload_hash"}
    return artifact_content_hash({key: value for key, value in payload.items() if key not in excluded})


def _source_commit(payload: dict[str, object]) -> bool:
    value = _text(payload, "source_commit")
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _date(payload: dict[str, object], name: str) -> date:
    try:
        return date.fromisoformat(_text(payload, name))
    except ValueError as exc:
        raise ValueError(f"Tomorrow V3 {name.replace('_', ' ')} is invalid") from exc


def _proxy_failure_reasons(payload: dict[str, object]) -> bool:
    reasons = tuple(_string_list(payload, "historical_failure_reasons"))
    return reasons == ("daily_close_proxy_not_point_in_time",)


def _decode_industries(
    payload: dict[str, object], feature_width: int
) -> tuple[tuple[str, V3IndustryModelArtifact], ...]:
    raw_industries = payload.get("industries")
    if not isinstance(raw_industries, dict) or not raw_industries:
        raise ValueError("Tomorrow V3 industry models are missing")
    industries = tuple(
        sorted(
            (_industry_name(industry), _industry_model(raw, feature_width)) for industry, raw in raw_industries.items()
        )
    )
    if len({industry for industry, _model in industries}) != len(industries):
        raise ValueError("Tomorrow V3 industry names must be unique")
    return industries


def _exposure_contract(payload: dict[str, object]) -> ExposureContract:
    raw = payload.get("exposure_contract")
    expected_keys = {"market", "board", "industry", "log_average_amount_20d", "order"}
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise TypeError("Tomorrow V3 exposure contract is invalid")
    for name in expected_keys - {"order"}:
        if raw.get(name) is not True:
            raise ValueError("Tomorrow V3 exposure contract is invalid")
    order = raw.get("order")
    if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
        raise TypeError("Tomorrow V3 exposure contract order is invalid")
    if tuple(order) != V3_EXPOSURE_CONTRACT.order:
        raise ValueError("Tomorrow V3 exposure contract is invalid")
    return V3_EXPOSURE_CONTRACT


def _ensemble_weights(payload: dict[str, object]) -> tuple[float, float]:
    raw = payload.get("ensemble_weights")
    if not isinstance(raw, dict) or set(raw) != {"ridge", "lightgbm"}:
        raise TypeError("Tomorrow V3 ensemble weights are invalid")
    ridge = _finite_number(raw.get("ridge"), "ridge ensemble weight")
    lightgbm = _finite_number(raw.get("lightgbm"), "LightGBM ensemble weight")
    if ridge != 0.5 or lightgbm != 0.5:
        raise ValueError("Tomorrow V3 ensemble weights are invalid")
    return ridge, lightgbm


def _industry_model(raw: object, width: int) -> V3IndustryModelArtifact:
    if not isinstance(raw, dict):
        raise TypeError("Tomorrow V3 industry model must be an object")
    values = cast(dict[str, object], raw)
    if set(values) != _INDUSTRY_MODEL_FIELDS:
        raise ValueError("Tomorrow V3 industry model fields are invalid")
    means = tuple(_number_list(values, "transformer_means"))
    scales = tuple(_number_list(values, "transformer_scales"))
    coefficients = tuple(_number_list(values, "ridge_coefficients"))
    numbers = (
        *means,
        *scales,
        _number(values, "ridge_intercept"),
        *coefficients,
        _number(values, "calibration_intercept"),
        _number(values, "calibration_slope"),
    )
    if (
        len(means) != width
        or len(scales) != width
        or len(coefficients) != width
        or any(not math.isfinite(item) for item in numbers)
        or any(item <= 0.0 for item in scales)
        or _integer(values, "lightgbm_best_iteration") < 1
        or _integer(values, "training_rows") < 1
        or _integer(values, "validation_rows") < 1
    ):
        raise ValueError("Tomorrow V3 industry model is invalid")
    return V3IndustryModelArtifact(
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
    )


def _dependencies(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
    raw = payload.get("dependencies")
    if (
        not isinstance(raw, dict)
        or set(raw) != {"lightgbm", "numpy"}
        or any(not isinstance(value, str) or not value for value in raw.values())
    ):
        raise ValueError("Tomorrow V3 dependency identity is invalid")
    return tuple(sorted(cast(dict[str, str], raw).items()))


def _industry_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError("Tomorrow V3 industry name must be non-empty text")
    return value.strip()


def _training_input_scope(payload: dict[str, object]) -> Literal["complete_manifest"]:
    value = _text(payload, "training_input_scope")
    if value != "complete_manifest":
        raise ValueError("Tomorrow V3 training input scope is invalid")
    return "complete_manifest"


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Tomorrow V3 model {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Tomorrow V3 model {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    return _finite_number(payload.get(name), name)


def _finite_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise TypeError(f"Tomorrow V3 model {name} must be finite numeric")
    return float(value)


def _boolean(payload: dict[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"Tomorrow V3 model {name} must be boolean")
    return value


def _sha256_text(payload: dict[str, object], name: str) -> bool:
    value = _text(payload, name)
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _string_list(payload: dict[str, object], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"Tomorrow V3 model {name} must be a string list")
    return cast(list[str], value)


def _number_list(payload: dict[str, object], name: str) -> list[float]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise TypeError(f"Tomorrow V3 model {name} must be a numeric list")
    return [_finite_number(item, name) for item in value]


__all__ = [
    "V3IndustryModelArtifact",
    "V3TomorrowBundleArtifact",
    "decode_tomorrow_bundle",
    "load_tomorrow_bundle",
]

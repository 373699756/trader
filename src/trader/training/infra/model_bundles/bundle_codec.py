"""Decode and validate one portable, hash-bound trained strategy-head bundle."""

from __future__ import annotations

from trader.training.infra.training_industry_metadata import ParseTrainingIndustrySnapshot, ValidateTrainingInputIdentity

import json
from dataclasses import dataclass, field
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
from trader.training.domain.final_fit import BuildFinalFitDatePlan, FINAL_FIT_CONTRACT, FinalFitDatePlan
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.domain.production_diagnostic_dates import (
    BuildProductionDiagnosticDatePlan,
    PRODUCTION_DIAGNOSTIC_CONTRACT,
    ProductionDiagnosticDatePlan,
)
from trader.training.infra.model_evaluation import (
    PREDICTION_EVALUATION_CONTRACT,
    RECENT_PREDICTION_EVALUATION_CONTRACT,
)
from trader.training.infra.model_bundles.evaluation_codec import (
    PREDICTION_REPORT_FIELDS,
    ValidatePredictionReport,
    ValidatePredictionSampleCounts,
)
from trader.training.infra.profile.v5.configuration import DecodeTcnConfiguration
from trader.training.infra.profile.v5.settings import TcnArchitecture
from trader.training.infra.profile.v5.tensors import DecodeTcnNetwork, TcnTensor


class ModelBundleIntegrityError(ValueError):
    """A model document does not match its declared content digest."""


class ModelBundleContractError(ValueError):
    """A verified model document conflicts with the inference contract."""


_MODEL_FIELDS = {"industries"}
_METADATA_FIELDS = {
    "schema_version",
    "profile_id",
    "model_id",
    "strategy_head",
    "feature_ids",
    "feature_units",
    "exposure_contract",
    "algorithm",
    "dependencies",
    "training_input",
    "validation_report",
    "industry_sample_counts",
    "model_hash",
}
_REPORT_COMMON_FIELDS = {
    "price_snapshot_hash",
    "industry_policy",
    "industry_content_hash",
    "schema_version",
    "profile_id",
    "model_id",
    "strategy_head",
    "training_input_scope",
    "training_input_hash",
    "training_run_hash",
    "label_cutoff",
    "source_identity_hash",
    "training_input_document_hash",
    "training_input_codes",
    "training_universe_codes",
    "feature_manifest_hash",
    "split_hash",
    "training_contract_hash",
    "model_hash",
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
    *PREDICTION_REPORT_FIELDS,
    "artifact_structure_valid",
    "artifact_structure_failure_reasons",
    "prediction_return_validation_passed",
    "prediction_return_failure_reasons",
}
_INPUT_FIELDS = {
    "price_snapshot_hash",
    "industry_policy",
    "industry_content_hash",
    "industry_snapshot",
    "schema_version",
    "profile_id",
    "strategy_head",
    "training_input_scope",
    "training_input_hash",
    "training_run_hash",
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
}


@dataclass(frozen=True)
class TrainedIndustryModelSnapshot:
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    calibration_intercept: float
    calibration_slope: float


@dataclass(frozen=True)
class TrainedRidgeModelSnapshot(TrainedIndustryModelSnapshot):
    ridge_intercept: float
    ridge_coefficients: tuple[float, ...]


@dataclass(frozen=True)
class TrainedLightGBMModelSnapshot(TrainedIndustryModelSnapshot):
    lightgbm_model: str
    lightgbm_best_iteration: int


@dataclass(frozen=True)
class TrainedTcnModelSnapshot(TrainedIndustryModelSnapshot):
    dropout: float
    weights: tuple[TcnTensor, ...]
    architecture: TcnArchitecture


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
    training_run_hash: str
    label_cutoff: date
    source_identity_hash: str
    training_input_document_hash: str
    training_input_codes: int
    training_universe_codes: int
    split_hash: str
    report_hash: str
    feature_manifest_hash: str
    training_contract_hash: str
    label_target: str
    historical_status: Literal["historical_data_insufficient"]
    historical_failure_reasons: tuple[str, ...]
    training_anchor: Literal["15:00_close_proxy"]
    runtime_anchor: Literal["15:00"]
    point_in_time_parity: Literal[False]
    training_rows: int
    validation_rows: int
    industries: tuple[
        tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot | TrainedTcnModelSnapshot], ...
    ]
    dependencies: tuple[tuple[str, str], ...]
    metadata_hash: str
    content_hash: str
    final_fit: FinalFitDatePlan | None = None
    diagnostic_plan: ProductionDiagnosticDatePlan | None = None
    price_snapshot_hash: str = field(kw_only=True)


@dataclass(frozen=True)
class _GroupIdentity:
    content_hash: str
    strategy: Strategy
    model_id: str
    training_input_hash: str
    training_run_hash: str
    label_cutoff: date
    source_identity_hash: str
    training_input_document_hash: str
    feature_manifest_hash: str
    split_hash: str
    training_contract_hash: str
    model_hash: str | None
    training_input_codes: int
    training_universe_codes: int
    industry_count: int | None
    training_rows: int | None
    validation_rows: int | None
    price_snapshot_hash: str
    industry_content_hash: str


def LoadHeadBundle(
    path: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> TrainedHeadModelBundle:
    return DecodeHeadBundle(
        json.loads(path.read_text(encoding="utf-8")),
        json.loads(path.with_name("metadata.json").read_text(encoding="utf-8")),
        strategy,
        profile,
    )


def DecodeHeadBundle(
    model_document: object,
    metadata_document: object,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> TrainedHeadModelBundle:
    model, declared_hash = _hashed_object(model_document, _MODEL_FIELDS, "model")
    legacy_refit_schema = f"{profile.profile_id}_head_refit_metadata"
    recent_schema = f"{profile.profile_id}_head_recent_refit_metadata"
    is_recent = isinstance(metadata_document, dict) and metadata_document.get("schema_version") == recent_schema
    is_final = isinstance(metadata_document, dict) and metadata_document.get("schema_version") in {
        legacy_refit_schema, recent_schema,
    }
    final_schema = recent_schema if is_recent else legacy_refit_schema
    metadata_fields = _METADATA_FIELDS | {"training_configuration"} if profile.algorithm == "tcn" else _METADATA_FIELDS
    if is_final:
        metadata_fields = metadata_fields | {"final_fit"}
    metadata, metadata_hash = _hashed_object(metadata_document, metadata_fields, "metadata")
    contract = profile.head_for_strategy(strategy)
    feature_ids = tuple(_string_list(metadata, "feature_ids"))
    feature_units = tuple(_string_list(metadata, "feature_units"))
    industries = _decode_industries(model, len(feature_ids), profile.algorithm)
    if profile.algorithm == "tcn":
        _, parameters = DecodeTcnConfiguration(metadata["training_configuration"])
        if any(
            not isinstance(item, TrainedTcnModelSnapshot)
            or item.architecture != parameters.architecture
            or item.dropout != parameters.dropout
            for _, item in industries
        ):
            raise ValueError("V5 model architecture conflicts with training configuration")
    dependencies = _dependencies(metadata, profile.algorithm)
    evaluation_contract = RECENT_PREDICTION_EVALUATION_CONTRACT if is_recent else PREDICTION_EVALUATION_CONTRACT
    training_input = _decode_group_document(
        metadata.get("training_input"), strategy, profile, report=False, evaluation_contract=evaluation_contract,
    )
    report = _decode_group_document(
        metadata.get("validation_report"), strategy, profile, report=True, evaluation_contract=evaluation_contract,
    )
    counts = metadata.get("industry_sample_counts")
    if not isinstance(counts, dict) or set(counts) != {name for name, _ in industries}:
        raise ValueError("trained-head industry sample counts are invalid")
    for values in counts.values():
        if (
            not isinstance(values, dict)
            or set(values) != {"training_rows", "validation_rows"}
            or _integer(values, "training_rows") < 1
            or _integer(values, "validation_rows") < 1
        ):
            raise ValueError("trained-head industry sample counts are invalid")
    try:
        ValidatePredictionSampleCounts(cast(dict[str, object], metadata["validation_report"]), counts)
    except ValueError as exc:
        raise ModelBundleContractError(
            "trained-head actual evaluation coverage conflicts; retraining is required"
        ) from exc
    final_rows = None
    final_plan = None
    diagnostic_plan = None
    diagnostic_hash = declared_hash
    if is_final:
        final_rows, diagnostic_hash, final_plan, diagnostic_plan = _decode_final_fit(
            metadata["final_fit"], metadata["training_input"], report, declared_hash, industries,
            profile, contract.maturity_sessions,
            report_calibrations=cast(dict[str, object], metadata["validation_report"])["calibration_metrics"],
            recent=is_recent,
        )
        if profile.algorithm == "tcn":
            configured_dates, _ = DecodeTcnConfiguration(metadata["training_configuration"])
            if len(final_plan.fit_dates) != configured_dates:
                raise ModelBundleContractError("final fit dates conflict with V5 training configuration")
        if is_recent:
            if diagnostic_plan is None:
                raise ModelBundleContractError("final fit recent diagnostic plan is missing")
            _validate_recent_report_window(
                cast(dict[str, object], metadata["validation_report"]), diagnostic_plan, report.training_input_codes,
            )
    if (
        _text(metadata, "schema_version") != (final_schema if is_final else f"{profile.profile_id}_head_metadata")
        or _text(metadata, "profile_id") != profile.profile_id
        or _text(metadata, "strategy_head") != strategy.value
        or _text(metadata, "model_id") != contract.model_id
        or _text(metadata, "algorithm") != profile.algorithm
        or _text(metadata, "model_hash") != declared_hash
        or feature_ids != contract.feature_manifest.names
        or feature_units != contract.feature_manifest.units
        or _exposure_contract(metadata) != TRAINED_HEAD_EXPOSURE_CONTRACT
        or len(industries) != report.industry_count
        or not industries
        or report.training_rows is None
        or report.training_rows < 1
        or report.validation_rows is None
        or report.validation_rows < 1
        or report.model_hash != diagnostic_hash
        or report.training_input_document_hash != training_input.content_hash
        or _identity_tuple(report) != _identity_tuple(training_input)
    ):
        raise ModelBundleContractError("trained strategy-head metadata or model contract is invalid")
    input_codes = report.training_input_codes
    universe_codes = report.training_universe_codes
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
        report.training_input_hash,
        report.training_run_hash,
        report.label_cutoff,
        report.source_identity_hash,
        training_input.content_hash,
        input_codes,
        universe_codes,
        report.split_hash,
        report.content_hash,
        report.feature_manifest_hash,
        report.training_contract_hash,
        contract.label_target,
        "historical_data_insufficient",
        ("daily_close_proxy_not_point_in_time",),
        "15:00_close_proxy",
        contract.runtime_anchor,
        False,
        final_rows if final_rows is not None else report.training_rows,
        report.validation_rows,
        industries,
        dependencies,
        metadata_hash,
        declared_hash,
        final_plan,
        diagnostic_plan,
        price_snapshot_hash=report.price_snapshot_hash,
    )


def _decode_final_fit(
    raw: object,
    raw_input: object,
    report: _GroupIdentity,
    model_hash: str,
    models: tuple[tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot | TrainedTcnModelSnapshot], ...],
    profile: TrainedProfileContract,
    maturity_sessions: int,
    *,
    report_calibrations: object,
    recent: bool,
) -> tuple[int, str, FinalFitDatePlan, ProductionDiagnosticDatePlan | None]:
    fields = {
        "contract", "source_cutoff", "calendar_dates", "maturity_sessions", "fit_dates", "plan_hash", "model_hash",
        "industry_training_rows", "diagnostic_model", "diagnostic_split_hash", "diagnostic_dates",
        "evaluation_model_role", "calibration_policy", "final_model_unseen_validation_passed", "research_holdout_status",
    }
    if recent:
        fields |= {"diagnostic_contract", "diagnostic_source_cutoff", "diagnostic_maturity_sessions"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ModelBundleContractError("final fit evidence fields are invalid")
    value = cast(dict[str, object], raw)
    if (
        value["contract"] != FINAL_FIT_CONTRACT
        or type(value["maturity_sessions"]) is not int
        or value["maturity_sessions"] != maturity_sessions
        or value["model_hash"] != model_hash
        or value["evaluation_model_role"] != "pre_refit_diagnostic"
        or value["calibration_policy"] != (
            "frozen_recent_diagnostic_model_transfer" if recent else "frozen_diagnostic_model_transfer"
        )
        or value["final_model_unseen_validation_passed"] is not False
        or value["research_holdout_status"] != (
            "not_part_of_recent_production_diagnostic"
            if recent else "may_be_used_by_final_refit_not_unseen_for_final_model"
        )
        or not isinstance(raw_input, dict)
        or value["source_cutoff"] != raw_input.get("source_cutoff")
    ):
        raise ModelBundleContractError("final fit evidence identity is invalid")
    plan = FinalFitDatePlan(
        _date(value, "source_cutoff"), maturity_sessions,
        tuple(date.fromisoformat(day) for day in _string_list(value, "fit_dates")),
    )
    if plan.content_hash != value["plan_hash"] or plan.fit_dates[-1] != report.label_cutoff:
        raise ModelBundleContractError("final fit does not end at the mature label cutoff")
    calendar_dates = tuple(date.fromisoformat(day) for day in _string_list(value, "calendar_dates"))
    if not (1 if recent else 1_250) <= len(calendar_dates) <= 2_000 or BaoStockCalendar(calendar_dates).content_hash != raw_input["calendar_hash"]:
        raise ModelBundleContractError("final fit source calendar binding is invalid")
    if recent and (
        raw_input["requested_sessions"] != len(calendar_dates)
        or plan.source_cutoff != calendar_dates[-1]
    ):
        raise ModelBundleContractError("final fit recent source calendar extent is invalid")
    if plan != BuildFinalFitDatePlan(
        calendar_dates, source_cutoff=plan.source_cutoff,
        fit_sessions=len(plan.fit_dates), maturity_sessions=maturity_sessions,
    ):
        raise ModelBundleContractError("final fit is not the latest mature calendar window")
    diagnostic, diagnostic_hash = _hashed_object(value["diagnostic_model"], _MODEL_FIELDS, "diagnostic model")
    evaluated = _decode_industries(diagnostic, len(profile.head_for_strategy(report.strategy).feature_manifest.names), profile.algorithm)
    if tuple(name for name, _ in evaluated) != tuple(name for name, _ in models):
        raise ModelBundleContractError("final fit diagnostic industry coverage is invalid")
    if recent and (not isinstance(report_calibrations, dict) or set(report_calibrations) != {name for name, _ in evaluated}):
        raise ModelBundleContractError("final fit recent calibration evidence is invalid")
    for (industry, old), (_industry, final) in zip(evaluated, models, strict=True):
        if (old.calibration_intercept, old.calibration_slope) != (final.calibration_intercept, final.calibration_slope):
            raise ModelBundleContractError("final fit calibration transfer is invalid")
        if recent:
            calibration = cast(dict[str, object], report_calibrations)[industry]
            if not isinstance(calibration, dict) or (
                calibration["intercept"], calibration["slope"]
            ) != (old.calibration_intercept, old.calibration_slope):
                raise ModelBundleContractError("final fit recent calibration report differs from its diagnostic model")
        if isinstance(old, TrainedLightGBMModelSnapshot) and isinstance(final, TrainedLightGBMModelSnapshot):
            if not 1 <= final.lightgbm_best_iteration <= old.lightgbm_best_iteration:
                raise ModelBundleContractError("final fit selected boosting rounds changed")
    diagnostic_plan = None
    if recent:
        diagnostic_plan = _recent_diagnostic_plan(value, calendar_dates, plan, profile)
        split_hash = diagnostic_plan.content_hash
    else:
        split_hash = _research_diagnostic_split(value, calendar_dates, plan, profile, report.price_snapshot_hash)
    if ContentHash({"calendar_split_hash": split_hash, "strategy_head": report.strategy.value,
                    "maturity_sessions": maturity_sessions,
                    "label_target": profile.head_for_strategy(report.strategy).label_target}) != report.split_hash:
        raise ModelBundleContractError("final fit diagnostic split binding is invalid")
    counts = value["industry_training_rows"]
    if not isinstance(counts, dict) or set(counts) != {name for name, _ in models}:
        raise ModelBundleContractError("final fit industry counts are invalid")
    if any(not IsInteger(count) or count < 1 for count in counts.values()):
        raise ModelBundleContractError("final fit row counts are invalid")
    return sum(cast(dict[str, int], counts).values()), diagnostic_hash, plan, diagnostic_plan


def _research_diagnostic_split(
    value: dict[str, object], calendar_dates: tuple[date, ...], plan: FinalFitDatePlan,
    profile: TrainedProfileContract, training_input_hash: str,
) -> str:
    dates = value["diagnostic_dates"]
    if not isinstance(dates, dict) or set(dates) != {
        "fitting", "early_stopping", "calibration", "validation", "count_only_holdout",
    }:
        raise ModelBundleContractError("final fit diagnostic dates are invalid")
    groups = tuple(
        tuple(date.fromisoformat(day) for day in _string_list(dates, name))
        for name in ("fitting", "early_stopping", "calibration", "validation", "count_only_holdout")
    )
    flattened = tuple(day for group in groups for day in group)
    if any(not group for position, group in enumerate(groups) if position != 1) or flattened != tuple(sorted(set(flattened))):
        raise ModelBundleContractError("final fit diagnostic dates must remain separate and ordered")
    _require_sha256(value, "diagnostic_split_hash")
    split_hash = value["diagnostic_split_hash"]
    expected_split = BuildTrainingDateSplit(
        tuple(day for day in calendar_dates if day <= plan.source_cutoff), parent_manifest_hash=training_input_hash,
        fit_dates=len(plan.fit_dates), recent_fit=profile.profile_id == "v5",
        early_stopping_sessions=20 if profile.algorithm in {"lightgbm", "tcn"} else 0,
        history_sessions=profile.history_sessions, history_excludes_signal=profile.algorithm == "tcn",
    )
    if split_hash != expected_split.content_hash or groups != (
        expected_split.model_fit_dates, expected_split.early_stopping_dates, expected_split.calibration_dates,
        expected_split.confirmation_dates, expected_split.daily_proxy_holdout_dates,
    ):
        raise ModelBundleContractError("final fit diagnostic calendar dates conflict with its split")
    return expected_split.content_hash


def _recent_diagnostic_plan(
    value: dict[str, object], calendar_dates: tuple[date, ...], plan: FinalFitDatePlan,
    profile: TrainedProfileContract,
) -> ProductionDiagnosticDatePlan:
    expected = BuildProductionDiagnosticDatePlan(
        calendar_dates, source_cutoff=plan.source_cutoff, fit_sessions=len(plan.fit_dates),
        early_stopping_sessions=20 if profile.algorithm in {"lightgbm", "tcn"} else 0,
        history_sessions=profile.history_sessions, history_excludes_signal=profile.algorithm == "tcn",
    )
    expected_dates = {
        "fitting": [day.isoformat() for day in expected.fitting_dates],
        "early_stopping": [day.isoformat() for day in expected.early_stopping_dates],
        "calibration": [day.isoformat() for day in expected.calibration_dates],
        "validation": [day.isoformat() for day in expected.validation_dates],
        "warmup": [day.isoformat() for day in expected.warmup_dates],
        "embargo": [[day.isoformat() for day in days] for days in expected.embargo_dates],
    }
    if (
        value["diagnostic_contract"] != PRODUCTION_DIAGNOSTIC_CONTRACT
        or value["diagnostic_source_cutoff"] != expected.source_cutoff.isoformat()
        or type(value["diagnostic_maturity_sessions"]) is not int
        or value["diagnostic_maturity_sessions"] != expected.maturity_sessions
        or value["diagnostic_split_hash"] != expected.content_hash
        or value["diagnostic_dates"] != expected_dates
    ):
        raise ModelBundleContractError("final fit recent diagnostic calendar provenance is invalid")
    return expected


def _validate_recent_report_window(
    report: dict[str, object], plan: ProductionDiagnosticDatePlan, codes: int,
) -> None:
    metrics = cast(dict[str, object], report["prediction_metrics"])
    coverage = cast(dict[str, object], report["evaluation_coverage"])
    calibration_metrics = cast(dict[str, dict[str, object]], report["calibration_metrics"])
    industry_metrics = cast(dict[str, dict[str, object]], report["industry_prediction_metrics"])
    if (
        _integer(metrics, "covered_dates") > len(plan.validation_dates)
        or _integer(metrics, "covered_codes") > codes
        or _integer(coverage, "candidate_training_rows") > len(plan.fitting_dates) * codes
        or _integer(coverage, "candidate_validation_rows") > len(plan.validation_dates) * codes
        or any(_integer(value, "dates") > len(plan.calibration_dates) for value in calibration_metrics.values())
        or any(_integer(value, "covered_dates") > len(plan.validation_dates) for value in industry_metrics.values())
    ):
        raise ModelBundleContractError("final fit recent evaluation coverage exceeds its diagnostic date plan")


def _decode_group_document(
    document: object,
    strategy: Strategy,
    profile: TrainedProfileContract,
    *,
    report: bool,
    evaluation_contract: str,
) -> _GroupIdentity:
    fields = _REPORT_FIELDS if report else _INPUT_FIELDS
    payload, declared_hash = _hashed_object(document, fields, "report" if report else "training input")
    ValidateTrainingInputIdentity(payload)
    contract = profile.head_for_strategy(strategy)
    expected_schema = (
        f"{profile.profile_id}_head_validation_report"
        if report
        else f"{profile.profile_id}_head_training_input_manifest"
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
        raise ModelBundleContractError("trained strategy-head group document contract is invalid")
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
    if report:
        try:
            ValidatePredictionReport(payload, evaluation_contract=evaluation_contract)
        except ValueError as exc:
            raise ModelBundleContractError(
                "trained-head evaluation contract conflicts; retraining is required"
            ) from exc
    if report and (
        not _boolean(payload, "artifact_structure_valid")
        or _string_list(payload, "artifact_structure_failure_reasons")
        or _boolean(payload, "prediction_return_validation_passed")
        or tuple(_string_list(payload, "prediction_return_failure_reasons")) != ("daily_close_proxy_not_point_in_time",)
    ):
        raise ValueError("trained strategy-head validation semantics are invalid")
    for name in (
        "training_input_hash",
        "training_run_hash",
        "source_identity_hash",
        "feature_manifest_hash",
        "split_hash",
        "training_contract_hash",
    ):
        _require_sha256(payload, name)
    model_hash = _text(payload, "model_hash") if report else None
    training_document_hash = _text(payload, "training_input_document_hash") if report else declared_hash
    if report:
        _require_sha256(payload, "training_input_document_hash")
        _require_sha256(payload, "model_hash")
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
        _text(payload, "training_run_hash"),
        _date(payload, "label_cutoff"),
        _text(payload, "source_identity_hash"),
        training_document_hash,
        _text(payload, "feature_manifest_hash"),
        _text(payload, "split_hash"),
        _text(payload, "training_contract_hash"),
        model_hash,
        input_codes,
        universe_codes,
        _integer(payload, "industry_count") if report else None,
        _integer(payload, "training_rows") if report else None,
        _integer(payload, "validation_rows") if report else None,
        _text(payload, "price_snapshot_hash"),
        _text(payload, "industry_content_hash"),
    )


def _validate_training_input(payload: dict[str, object], input_codes: int) -> None:
    for name in ("calendar_hash", "input_descriptor_hash"):
        _require_sha256(payload, name)
    source_cutoff = _date(payload, "source_cutoff")
    if _date(payload, "label_cutoff") >= source_cutoff:
        raise ValueError("trained-head input label cutoff is invalid")
    requested_sessions = _integer(payload, "requested_sessions")
    codes = _string_list(payload, "codes")
    industry = ParseTrainingIndustrySnapshot(payload["industry_snapshot"])
    if (
        industry.content_hash != payload["industry_content_hash"]
        or sorted((*[member.code for member in industry.members], *[gap.code for gap in industry.gaps])) != codes
    ):
        raise ModelBundleContractError("trained-head current industry provenance conflicts with its input population")
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
        value.training_run_hash,
        value.label_cutoff,
        value.source_identity_hash,
        value.feature_manifest_hash,
        value.split_hash,
        value.training_contract_hash,
        value.training_input_codes,
        value.training_universe_codes,
        value.price_snapshot_hash,
        value.industry_content_hash,
    )


def _hashed_object(document: object, fields: set[str], label: str) -> tuple[dict[str, object], str]:
    if not isinstance(document, dict):
        raise TypeError(f"trained-head {label} must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    declared_hash = payload.pop("content_hash", None)
    if not isinstance(declared_hash, str) or ContentHash(payload) != declared_hash:
        raise ModelBundleIntegrityError(f"trained-head {label} content hash is invalid")
    if set(payload) != fields:
        raise ModelBundleContractError(f"trained-head {label} fields are invalid")
    return payload, declared_hash


def _decode_industries(
    payload: dict[str, object], width: int, algorithm: TrainedAlgorithm
) -> tuple[tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot | TrainedTcnModelSnapshot], ...]:
    raw = payload.get("industries")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("trained-head industry models are missing")
    result: list[tuple[str, TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot | TrainedTcnModelSnapshot]] = []
    model_fields = (
        {"lightgbm_model", "lightgbm_best_iteration"}
        if algorithm == "lightgbm"
        else {"tcn_network"}
        if algorithm == "tcn"
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
        if len(means) != width or len(scales) != width or any(value <= 0.0 for value in scales):
            raise ValueError("trained-head industry model dimensions are invalid")
        calibration_intercept = _number(values, "calibration_intercept")
        calibration_slope = _number(values, "calibration_slope")
        if algorithm == "lightgbm":
            if _integer(values, "lightgbm_best_iteration") < 1:
                raise ValueError("trained-head LightGBM iteration is invalid")
            model: TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot | TrainedTcnModelSnapshot = (
                TrainedLightGBMModelSnapshot(
                    transformer_means=means,
                    transformer_scales=scales,
                    calibration_intercept=calibration_intercept,
                    calibration_slope=calibration_slope,
                    lightgbm_model=_text(values, "lightgbm_model"),
                    lightgbm_best_iteration=_integer(values, "lightgbm_best_iteration"),
                )
            )
        elif algorithm == "tcn":
            dropout, weights, architecture = DecodeTcnNetwork(values["tcn_network"], width - 360)
            model = TrainedTcnModelSnapshot(
                means, scales, calibration_intercept, calibration_slope, dropout, weights, architecture
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
    expected = (
        {"numpy", "lightgbm"} if algorithm == "lightgbm" else {"numpy", "torch"} if algorithm == "tcn" else {"numpy"}
    )
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
    "ModelBundleContractError",
    "ModelBundleIntegrityError",
    "TrainedHeadModelBundle",
    "TrainedIndustryModelSnapshot",
    "TrainedRidgeModelSnapshot",
    "TrainedTcnModelSnapshot",
    "TrainedLightGBMModelSnapshot",
    "DecodeHeadBundle",
    "LoadHeadBundle",
]

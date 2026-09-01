"""Strict canonical JSON codec for Tomorrow daily-close research artifacts."""

from __future__ import annotations

import json
from datetime import date
from typing import cast

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.tomorrow_daily_close_training import (
    BaseModelKind,
    CandidateModelArtifact,
    CorrectionDimension,
    DailyCloseBoard,
    DailyCloseFeatureRow,
    DailyCloseTrainingStatus,
    DatasetManifest,
    FeatureDataset,
    ModelDependencyVersion,
    StockResidualCorrection,
    StratumCorrection,
    ValidationMetrics,
    ValidationReport,
)

TomorrowDailyCloseArtifact = DatasetManifest | FeatureDataset | ValidationReport | CandidateModelArtifact


class TomorrowDailyCloseArtifactError(ValueError):
    pass


class TomorrowDailyCloseArtifactCodec:
    """Encode and reconstruct typed artifacts while rejecting schema drift or tampering."""

    @staticmethod
    def encode(artifact: TomorrowDailyCloseArtifact) -> str:
        payload = canonical_value(artifact)
        if not isinstance(payload, dict):
            raise TypeError("Tomorrow daily-close artifact must encode to an object")
        payload["content_hash"] = artifact.content_hash
        return canonical_json(payload)

    @classmethod
    def decode_manifest(cls, encoded: str) -> DatasetManifest:
        raw = cls._verified_object(encoded)
        try:
            cls._exact_schema(raw, _MANIFEST_FIELDS)
            manifest = cls._manifest(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise TomorrowDailyCloseArtifactError("Tomorrow daily-close manifest schema is invalid") from exc
        cls._same_hash(raw, manifest.content_hash)
        return manifest

    @classmethod
    def decode_feature_dataset(cls, encoded: str) -> FeatureDataset:
        raw = cls._verified_object(encoded)
        try:
            cls._exact_schema(raw, _DATASET_FIELDS)
            manifest_raw = _object(raw["manifest"])
            cls._exact_schema(manifest_raw, _MANIFEST_FIELDS - {"content_hash"})
            manifest = cls._manifest(manifest_raw)
            rows = tuple(cls._feature_row(_object(item)) for item in _list(raw["rows"]))
            dataset = FeatureDataset(
                manifest=manifest,
                rows=rows,
                schema_version=_string(raw["schema_version"]),
                production_authority=_bool(raw["production_authority"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TomorrowDailyCloseArtifactError("Tomorrow daily-close feature dataset schema is invalid") from exc
        cls._same_hash(raw, dataset.content_hash)
        return dataset

    @classmethod
    def decode_validation_report(cls, encoded: str) -> ValidationReport:
        raw = cls._verified_object(encoded)
        try:
            cls._exact_schema(raw, _REPORT_FIELDS)
            metrics_raw = _object(raw["metrics"])
            cls._exact_schema(metrics_raw, _METRICS_FIELDS)
            report = ValidationReport(
                status=cast(DailyCloseTrainingStatus, _string(raw["status"])),
                manifest_hash=_string(raw["manifest_hash"]),
                candidate_model_artifact_hash=_optional_string(raw["candidate_model_artifact_hash"]),
                metrics=ValidationMetrics(
                    evaluated_trade_dates=_int(metrics_raw["evaluated_trade_dates"]),
                    evaluated_rows=_int(metrics_raw["evaluated_rows"]),
                    net_excess_return_20bp=_optional_float(metrics_raw["net_excess_return_20bp"]),
                    net_excess_return_50bp=_optional_float(metrics_raw["net_excess_return_50bp"]),
                    bootstrap_lower_bound_20bp=_optional_float(metrics_raw["bootstrap_lower_bound_20bp"]),
                    bootstrap_lower_bound_50bp=_optional_float(metrics_raw["bootstrap_lower_bound_50bp"]),
                    control_severe_loss_rate=_optional_float(metrics_raw["control_severe_loss_rate"]),
                    candidate_severe_loss_rate=_optional_float(metrics_raw["candidate_severe_loss_rate"]),
                    turnover_increase=_optional_float(metrics_raw["turnover_increase"]),
                    rank_ic=_optional_float(metrics_raw["rank_ic"]),
                    top_bottom_quintile_spread=_optional_float(metrics_raw["top_bottom_quintile_spread"]),
                ),
                failure_reasons=_strings(raw["failure_reasons"]),
                research_identity=_string(raw["research_identity"]),
                proxy_anchor=_string(raw["proxy_anchor"]),
                schema_version=_string(raw["schema_version"]),
                production_authority=_bool(raw["production_authority"]),
                automatic_model_update=_bool(raw["automatic_model_update"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TomorrowDailyCloseArtifactError("Tomorrow daily-close validation report schema is invalid") from exc
        cls._same_hash(raw, report.content_hash)
        return report

    @classmethod
    def decode_candidate_model(cls, encoded: str) -> CandidateModelArtifact:
        raw = cls._verified_object(encoded)
        try:
            cls._exact_schema(raw, _MODEL_FIELDS)
            artifact = CandidateModelArtifact(
                model_id=_string(raw["model_id"]),
                candidate_id=_string(raw["candidate_id"]),
                base_model_kind=cast(BaseModelKind, _string(raw["base_model_kind"])),
                manifest_hash=_string(raw["manifest_hash"]),
                filter_spec_hash=_string(raw["filter_spec_hash"]),
                confirmation_report_hash=_string(raw["confirmation_report_hash"]),
                feature_names=_strings(raw["feature_names"]),
                feature_units=_strings(raw["feature_units"]),
                preprocessing_means=_floats(raw["preprocessing_means"]),
                preprocessing_scales=_floats(raw["preprocessing_scales"]),
                ridge_intercept=_optional_float(raw["ridge_intercept"]),
                ridge_coefficients=_optional_floats(raw["ridge_coefficients"]),
                lightgbm_model_text=_optional_string(raw["lightgbm_model_text"]),
                lightgbm_best_iteration=_optional_int(raw["lightgbm_best_iteration"]),
                stratum_corrections=tuple(
                    cls._stratum_correction(_object(item)) for item in _list(raw["stratum_corrections"])
                ),
                stock_residual_corrections=tuple(
                    cls._stock_correction(_object(item)) for item in _list(raw["stock_residual_corrections"])
                ),
                trained_from=_date(raw["trained_from"]),
                trained_through=_date(raw["trained_through"]),
                dependencies=tuple(cls._dependency(_object(item)) for item in _list(raw["dependencies"])),
                schema_version=_string(raw["schema_version"]),
                production_authority=_bool(raw["production_authority"]),
                automatic_model_update=_bool(raw["automatic_model_update"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TomorrowDailyCloseArtifactError("Tomorrow daily-close candidate model schema is invalid") from exc
        cls._same_hash(raw, artifact.content_hash)
        return artifact

    @staticmethod
    def _verified_object(encoded: str) -> dict[str, object]:
        try:
            raw = json.loads(encoded)
            if not isinstance(raw, dict):
                raise TypeError("artifact payload is not an object")
            payload = {str(key): value for key, value in raw.items()}
            stored_hash = payload.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(payload) != stored_hash:
                raise ValueError("artifact content hash mismatch")
            payload["content_hash"] = stored_hash
            return payload
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowDailyCloseArtifactError("Tomorrow daily-close artifact hash is invalid") from exc

    @staticmethod
    def _exact_schema(raw: dict[str, object], expected: frozenset[str]) -> None:
        if frozenset(raw) != expected:
            raise ValueError("artifact fields do not match the registered schema")

    @staticmethod
    def _same_hash(raw: dict[str, object], reconstructed_hash: str) -> None:
        if raw["content_hash"] != reconstructed_hash:
            raise TomorrowDailyCloseArtifactError("Tomorrow daily-close reconstructed hash is invalid")

    @staticmethod
    def _manifest(raw: dict[str, object]) -> DatasetManifest:
        return DatasetManifest(
            source_archive_hash=_string(raw["source_archive_hash"]),
            filter_spec_hash=_string(raw["filter_spec_hash"]),
            trading_dates=_dates(raw["trading_dates"]),
            feature_names=_strings(raw["feature_names"]),
            feature_units=_strings(raw["feature_units"]),
            source_rows=_int(raw["source_rows"]),
            accepted_rows=_int(raw["accepted_rows"]),
            rejected_filter_evidence_rows=_int(raw["rejected_filter_evidence_rows"]),
            rejected_hard_filter_rows=_int(raw["rejected_hard_filter_rows"]),
            research_identity=_string(raw["research_identity"]),
            label_id=_string(raw["label_id"]),
            proxy_anchor=_string(raw["proxy_anchor"]),
            cost_rates=cast(tuple[float, float, float], _floats(raw["cost_rates"])),
            schema_version=_string(raw["schema_version"]),
            production_authority=_bool(raw["production_authority"]),
        )

    @staticmethod
    def _feature_row(raw: dict[str, object]) -> DailyCloseFeatureRow:
        TomorrowDailyCloseArtifactCodec._exact_schema(raw, _FEATURE_ROW_FIELDS)
        return DailyCloseFeatureRow(
            trade_date=_date(raw["trade_date"]),
            label_maturity_date=_date(raw["label_maturity_date"]),
            code=_string(raw["code"]),
            board=cast(DailyCloseBoard, _string(raw["board"])),
            feature_values=_floats(raw["feature_values"]),
            net_excess_returns=cast(tuple[float, float, float], _floats(raw["net_excess_returns"])),
            filter_evidence_hash=_string(raw["filter_evidence_hash"]),
            source_row_hash=_string(raw["source_row_hash"]),
        )

    @staticmethod
    def _stratum_correction(raw: dict[str, object]) -> StratumCorrection:
        TomorrowDailyCloseArtifactCodec._exact_schema(raw, _STRATUM_FIELDS)
        return StratumCorrection(
            dimension=cast(CorrectionDimension, _string(raw["dimension"])),
            key=_string(raw["key"]),
            sample_count=_int(raw["sample_count"]),
            minimum_sample_count=_int(raw["minimum_sample_count"]),
            shrinkage_constant=_float(raw["shrinkage_constant"]),
            correction=_float(raw["correction"]),
        )

    @staticmethod
    def _stock_correction(raw: dict[str, object]) -> StockResidualCorrection:
        TomorrowDailyCloseArtifactCodec._exact_schema(raw, _STOCK_FIELDS)
        return StockResidualCorrection(
            code=_string(raw["code"]),
            sample_count=_int(raw["sample_count"]),
            distinct_trade_dates=_int(raw["distinct_trade_dates"]),
            shrinkage_constant=_float(raw["shrinkage_constant"]),
            prediction_cross_section_stddev=_float(raw["prediction_cross_section_stddev"]),
            correction=_float(raw["correction"]),
        )

    @staticmethod
    def _dependency(raw: dict[str, object]) -> ModelDependencyVersion:
        TomorrowDailyCloseArtifactCodec._exact_schema(raw, _DEPENDENCY_FIELDS)
        return ModelDependencyVersion(name=_string(raw["name"]), version=_string(raw["version"]))


_MANIFEST_FIELDS = frozenset(
    {
        "source_archive_hash",
        "filter_spec_hash",
        "trading_dates",
        "feature_names",
        "feature_units",
        "source_rows",
        "accepted_rows",
        "rejected_filter_evidence_rows",
        "rejected_hard_filter_rows",
        "research_identity",
        "label_id",
        "proxy_anchor",
        "cost_rates",
        "schema_version",
        "production_authority",
        "content_hash",
    }
)
_FEATURE_ROW_FIELDS = frozenset(
    {
        "trade_date",
        "label_maturity_date",
        "code",
        "board",
        "feature_values",
        "net_excess_returns",
        "filter_evidence_hash",
        "source_row_hash",
    }
)
_DATASET_FIELDS = frozenset({"manifest", "rows", "schema_version", "production_authority", "content_hash"})
_METRICS_FIELDS = frozenset(
    {
        "evaluated_trade_dates",
        "evaluated_rows",
        "net_excess_return_20bp",
        "net_excess_return_50bp",
        "bootstrap_lower_bound_20bp",
        "bootstrap_lower_bound_50bp",
        "control_severe_loss_rate",
        "candidate_severe_loss_rate",
        "turnover_increase",
        "rank_ic",
        "top_bottom_quintile_spread",
    }
)
_REPORT_FIELDS = frozenset(
    {
        "status",
        "manifest_hash",
        "candidate_model_artifact_hash",
        "metrics",
        "failure_reasons",
        "research_identity",
        "proxy_anchor",
        "schema_version",
        "production_authority",
        "automatic_model_update",
        "content_hash",
    }
)
_STRATUM_FIELDS = frozenset(
    {"dimension", "key", "sample_count", "minimum_sample_count", "shrinkage_constant", "correction"}
)
_STOCK_FIELDS = frozenset(
    {
        "code",
        "sample_count",
        "distinct_trade_dates",
        "shrinkage_constant",
        "prediction_cross_section_stddev",
        "correction",
    }
)
_DEPENDENCY_FIELDS = frozenset({"name", "version"})
_MODEL_FIELDS = frozenset(
    {
        "model_id",
        "candidate_id",
        "base_model_kind",
        "manifest_hash",
        "filter_spec_hash",
        "confirmation_report_hash",
        "feature_names",
        "feature_units",
        "preprocessing_means",
        "preprocessing_scales",
        "ridge_intercept",
        "ridge_coefficients",
        "lightgbm_model_text",
        "lightgbm_best_iteration",
        "stratum_corrections",
        "stock_residual_corrections",
        "trained_from",
        "trained_through",
        "dependencies",
        "schema_version",
        "production_authority",
        "automatic_model_update",
        "content_hash",
    }
)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("artifact value must be an object with string keys")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("artifact value must be an array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("artifact value must be a string")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("artifact value must be a boolean")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("artifact value must be an integer")
    return value


def _float(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("artifact value must be numeric")
    return float(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else _float(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _list(value))


def _floats(value: object) -> tuple[float, ...]:
    return tuple(_float(item) for item in _list(value))


def _optional_floats(value: object) -> tuple[float, ...] | None:
    return None if value is None else _floats(value)


def _date(value: object) -> date:
    return date.fromisoformat(_string(value))


def _dates(value: object) -> tuple[date, ...]:
    return tuple(_date(item) for item in _list(value))


__all__ = [
    "TomorrowDailyCloseArtifact",
    "TomorrowDailyCloseArtifactCodec",
    "TomorrowDailyCloseArtifactError",
]

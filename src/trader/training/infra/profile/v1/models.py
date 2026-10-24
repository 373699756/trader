"""V1 model publication, validation and already-loaded inference."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import lightgbm as lgb
import numpy as np
from lightgbm.basic import LightGBMError

from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.serialization.canonical import CanonicalValue, ContentHash
from trader.infra.serialization.fields import IsFiniteNumber, IsInteger, IsNonEmptyText, IsTextSequence
from trader.infra.settings.parser import RequireExactKeys
from trader.recommendation.application.ports.loaded_profile import (
    HeadRuntime,
    LoadedScoringProfile,
    ModelInput,
    ModelPrediction,
    ProfileEvidence,
)
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import LEARNED_RANK_SCORE_SCALE, ScoringProfileId
from trader.training.domain.model_publication import ModelPublicationIdentity
from trader.training.domain.v1.learned_rank import (
    LearnedRankDateSplit,
    LearnedRankHead,
    LearnedRankPair,
    LearnedRankPredictionMetrics,
)
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.model_bundles.profile_publication import ProfilePublicationLock
from trader.training.infra.profile.composition import SingleHeadCombiner
from trader.training.infra.profile.v1.settings import ParseV1TrainingSettings

_MAXIMUM_MODEL_DOCUMENT_BYTES = 32 * 1024 * 1024
_STRATEGIES = (Strategy.TOMORROW, Strategy.D25)
_MODEL_FIELDS = frozenset(
    {
        "transformer_means",
        "transformer_scales",
        "ridge_coefficients",
        "ridge_intercept",
        "calibration_intercept",
        "calibration_slope",
        "rank_model",
        "rank_iterations",
    }
)
_HEAD_INPUT_FIELDS = frozenset(
    {
        "training_contract_hash",
        "split",
        "training_rows",
        "early_stopping_rows",
        "calibration_rows",
        "industries",
    }
)
_SHARED_INPUT_FIELDS = frozenset(
    {"training_input_hash", "source_identity_hash", "calendar_hash", "source_cutoff", "settings"}
)
_MODEL_DOCUMENT_FIELDS = frozenset(
    {"schema_version", "profile_id", "strategy_head", "pair_content_hash", "head_content_hash", "components"}
)
_METADATA_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "profile_id",
        "strategy_head",
        "model_id",
        "score_scale",
        "pair_content_hash",
        "head_content_hash",
        "model_hash",
        "contract_hash",
        "feature_ids",
        "algorithm",
        "training_input",
        "validation_report",
    }
)
_VALIDATION_REPORT_FIELDS = frozenset(
    {
        "validation",
        "production_authority",
        "prediction_return_validation_passed",
        "point_in_time_parity",
        "feature_anchor",
        "label_anchor",
    }
)
_SHARED_METADATA_FIELDS = _METADATA_DOCUMENT_FIELDS - {
    "strategy_head",
    "head_content_hash",
    "model_hash",
    "training_input",
    "validation_report",
}
_LOGGER = logging.getLogger(__name__)


class LearnedRankPredictor:
    """No filesystem, model fitting or per-stock predictor construction on this path."""

    def __init__(self, head: LearnedRankHead) -> None:
        self._head = head
        self._means = np.asarray(head.transformer_means, dtype=np.float64)
        self._scales = np.asarray(head.transformer_scales, dtype=np.float64)
        self._coefficients = np.asarray(head.ridge_coefficients, dtype=np.float64)
        try:
            self._ranker = lgb.Booster(model_str=head.rank_model)
        except LightGBMError as exc:
            raise ValueError("learned-rank ranking component is invalid") from exc
        if (
            self._ranker.num_feature() != len(LEARNED_RANK_FEATURE_IDS)
            or self._ranker.current_iteration() != head.rank_iterations
        ):
            raise ValueError("learned-rank ranking component contract differs")

    @property
    def profile_id(self) -> ScoringProfileId:
        return "v1"

    @property
    def model_id(self) -> str:
        return "learned_rank"

    @property
    def model_hash(self) -> str:
        return self._head.content_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return LEARNED_RANK_FEATURE_IDS

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        if not inputs:
            return ()
        if len({item.code for item in inputs}) != len(inputs) or any(
            len(item.alpha_features) != len(self.feature_ids) for item in inputs
        ):
            raise ValueError("learned-rank inputs must have unique codes and the exact feature width")
        matrix = np.asarray([item.alpha_features for item in inputs], dtype=np.float64)
        matrix -= self._means
        matrix /= self._scales
        rank_raw = self._ranker.predict(matrix, num_iteration=self._head.rank_iterations, num_threads=1)
        gross = self._head.calibration_intercept + self._head.calibration_slope * (
            self._head.ridge_intercept + matrix @ self._coefficients
        )
        return tuple(
            ModelPrediction(item.code, float(prediction), float(rank))
            for item, prediction, rank in zip(inputs, gross, rank_raw, strict=True)
        )


def PublishLearnedRankPair(model_root: Path, pair: LearnedRankPair) -> None:
    """Publish two validated head bundles as one locked profile directory."""
    for head in pair.heads:
        LearnedRankPredictor(head)
    model_root.mkdir(parents=True, exist_ok=True)
    profile_root = model_root / "v1"
    if profile_root.is_symlink() or (profile_root.exists() and not profile_root.is_dir()):
        raise ValueError("V1 model directory is invalid")
    staging = Path(tempfile.mkdtemp(prefix=".v1.publication.", dir=model_root))
    previous = model_root / f".v1.previous.{uuid.uuid4().hex}"
    old_moved = False
    try:
        for head in pair.heads:
            directory = staging / head.strategy.value
            values = _object(CanonicalValue(head))
            model = {
                "schema_version": "learned_rank_head_model",
                "profile_id": "v1",
                "strategy_head": head.strategy.value,
                "pair_content_hash": pair.content_hash,
                "head_content_hash": head.content_hash,
                "components": {key: values[key] for key in _MODEL_FIELDS},
            }
            model["content_hash"] = ContentHash(model)
            metadata = {
                "schema_version": "learned_rank_head_metadata",
                "profile_id": "v1",
                "strategy_head": head.strategy.value,
                "model_id": "learned_rank",
                "score_scale": LEARNED_RANK_SCORE_SCALE,
                "pair_content_hash": pair.content_hash,
                "head_content_hash": head.content_hash,
                "model_hash": model["content_hash"],
                "contract_hash": pair.contract_hash,
                "feature_ids": list(LEARNED_RANK_FEATURE_IDS),
                "algorithm": "lightgbm_lambdarank_ridge",
                "training_input": {
                    "training_input_hash": pair.training_input_hash,
                    "source_identity_hash": pair.source_identity_hash,
                    "calendar_hash": pair.calendar_hash,
                    "source_cutoff": pair.source_cutoff.isoformat(),
                    "settings": CanonicalValue(pair.settings),
                    **{key: values[key] for key in _HEAD_INPUT_FIELDS},
                },
                "validation_report": {
                    "validation": values["validation"],
                    "production_authority": False,
                    "prediction_return_validation_passed": False,
                    "point_in_time_parity": False,
                    "feature_anchor": "previous_completed_session",
                    "label_anchor": "signal_close_proxy",
                },
            }
            metadata["content_hash"] = ContentHash(metadata)
            if any(
                len(json.dumps(document).encode("utf-8")) > _MAXIMUM_MODEL_DOCUMENT_BYTES
                for document in (model, metadata)
            ):
                raise ValueError("V1 model document exceeds its size limit")
            AtomicWriteJson(directory / "model.json", model)
            AtomicWriteJson(directory / "metadata.json", metadata)
            _fsync_directory(directory)
        _fsync_directory(staging)
        if _read_pair_directory(staging) != pair:
            raise ValueError("learned-rank staged pair differs")
        with ProfilePublicationLock(profile_root):
            if profile_root.is_symlink() or (profile_root.exists() and not profile_root.is_dir()):
                raise ValueError("V1 model directory is invalid")
            if profile_root.exists():
                if (profile_root / "model.json").exists() or (profile_root / "model.json").is_symlink():
                    raise ValueError("retired V1 model directory cannot be replaced")
                _require_replaceable_directory(profile_root)
                os.replace(profile_root, previous)
                old_moved = True
            new_moved = False
            try:
                os.replace(staging, profile_root)
                new_moved = True
                _fsync_directory(model_root)
            except OSError:
                if new_moved:
                    os.replace(profile_root, staging)
                if old_moved:
                    os.replace(previous, profile_root)
                    old_moved = False
                raise
        if old_moved:
            try:
                shutil.rmtree(previous)
            except OSError:
                _LOGGER.warning("V1 previous model directory cleanup failed")
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _require_replaceable_directory(profile_root: Path) -> None:
    """Check ownership without requiring obsolete or damaged models to load."""
    for directory in profile_root.iterdir():
        if (
            directory.name not in {strategy.value for strategy in _STRATEGIES}
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            raise ValueError("V1 model directory has unexpected entries")
        for path in directory.iterdir():
            if path.name not in {"model.json", "metadata.json"} or path.is_symlink() or not path.is_file():
                raise ValueError("V1 head directory has unexpected entries")


def ReadLearnedRankPair(model_root: Path) -> LearnedRankPair:
    profile_root = model_root / "v1"
    if not model_root.exists():
        raise FileNotFoundError(profile_root)
    with ProfilePublicationLock(profile_root):
        return _read_pair_directory(profile_root)


def _read_pair_directory(profile_root: Path) -> LearnedRankPair:
    if not profile_root.exists() and not profile_root.is_symlink():
        raise FileNotFoundError(profile_root)
    if not profile_root.is_dir() or profile_root.is_symlink():
        raise ValueError("V1 model directory is invalid")
    head_names = {path.name for path in profile_root.iterdir()}
    expected_heads = {strategy.value for strategy in _STRATEGIES}
    if head_names < expected_heads:
        raise FileNotFoundError("V1 model requires both heads")
    if head_names != expected_heads:
        raise ValueError("V1 model directory has unexpected entries")
    documents: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for strategy in _STRATEGIES:
        directory = profile_root / strategy.value
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("V1 head directory is invalid")
        file_names = {path.name for path in directory.iterdir()}
        expected_files = {"model.json", "metadata.json"}
        if file_names < expected_files:
            raise FileNotFoundError("V1 head files are missing")
        if file_names != expected_files:
            raise ValueError("V1 head directory has unexpected entries")
        paths = (directory / "model.json", directory / "metadata.json")
        if any(
            path.is_symlink() or not path.is_file() or path.stat().st_size > _MAXIMUM_MODEL_DOCUMENT_BYTES
            for path in paths
        ):
            raise ValueError("learned-rank head path or size is invalid")
        model, metadata = (_object(json.loads(path.read_text(encoding="utf-8"))) for path in paths)
        _verify_document(model, _MODEL_DOCUMENT_FIELDS, "V1 model")
        _verify_document(metadata, _METADATA_DOCUMENT_FIELDS, "V1 metadata")
        expected: Mapping[str, object] = {
            "profile_id": "v1",
            "strategy_head": strategy.value,
            "pair_content_hash": model["pair_content_hash"],
            "head_content_hash": model["head_content_hash"],
        }
        if metadata["model_hash"] != model["content_hash"]:
            raise ModelBundleIntegrityError("V1 model and metadata hash differ")
        if (
            model["schema_version"] != "learned_rank_head_model"
            or metadata["schema_version"] != "learned_rank_head_metadata"
            or any(model[key] != value or metadata[key] != value for key, value in expected.items())
        ):
            raise ModelBundleContractError("V1 head identity differs")
        documents.append((model, metadata))
    first = documents[0][1]
    expected_metadata: Mapping[str, object] = {
        "model_id": "learned_rank",
        "score_scale": LEARNED_RANK_SCORE_SCALE,
        "feature_ids": list(LEARNED_RANK_FEATURE_IDS),
        "algorithm": "lightgbm_lambdarank_ridge",
    }
    if any(type(first[key]) is not type(value) or first[key] != value for key, value in expected_metadata.items()):
        raise ModelBundleContractError("V1 model identity is invalid")
    if any(metadata[key] != first[key] for _, metadata in documents[1:] for key in _SHARED_METADATA_FIELDS):
        raise ModelBundleContractError("V1 pair metadata differs")
    expected_report: Mapping[str, object] = {
        "production_authority": False,
        "prediction_return_validation_passed": False,
        "point_in_time_parity": False,
        "feature_anchor": "previous_completed_session",
        "label_anchor": "signal_close_proxy",
    }
    heads: list[LearnedRankHead] = []
    inputs: list[Mapping[str, object]] = []
    for model, metadata in documents:
        components = _object(model["components"])
        training = _object(metadata["training_input"])
        report = _object(metadata["validation_report"])
        RequireExactKeys(components, set(_MODEL_FIELDS), "learned_rank_components")
        RequireExactKeys(training, set(_SHARED_INPUT_FIELDS | _HEAD_INPUT_FIELDS), "learned_rank_training_input")
        RequireExactKeys(report, set(_VALIDATION_REPORT_FIELDS), "learned_rank_validation_report")
        if any(type(report[key]) is not type(value) or report[key] != value for key, value in expected_report.items()):
            raise ModelBundleContractError("V1 validation report identity is invalid")
        head = _head(
            {
                "strategy": metadata["strategy_head"],
                **components,
                **{key: training[key] for key in _HEAD_INPUT_FIELDS},
                "validation": report["validation"],
            }
        )
        if head.content_hash != metadata["head_content_hash"]:
            raise ModelBundleIntegrityError("V1 head hash differs")
        heads.append(head)
        inputs.append(training)
    if any(inputs[1][key] != inputs[0][key] for key in _SHARED_INPUT_FIELDS):
        raise ModelBundleContractError("V1 pair training input differs")
    first_input = inputs[0]
    pair = LearnedRankPair(
        _text(first_input, "training_input_hash"),
        _text(first_input, "source_identity_hash"),
        _text(first_input, "calendar_hash"),
        date.fromisoformat(_text(first_input, "source_cutoff")),
        ParseV1TrainingSettings(_object(first_input["settings"])),
        (heads[0], heads[1]),
    )
    if first["pair_content_hash"] != pair.content_hash:
        raise ModelBundleIntegrityError("V1 pair content hash differs")
    if first["contract_hash"] != pair.contract_hash:
        raise ModelBundleContractError("V1 pair contract hash differs")
    return pair


def _verify_document(raw: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    RequireExactKeys(raw, set(fields) | {"content_hash"}, label)
    if raw["content_hash"] != ContentHash({key: raw[key] for key in fields}):
        raise ModelBundleIntegrityError(f"{label} content hash differs")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def LoadV1ScoringProfile(model_root: Path) -> LoadedScoringProfile:
    pair = ReadLearnedRankPair(model_root)
    evidence = ProfileEvidence(
        "historical_unavailable",
        ("prediction_return_validation_not_passed", "point_in_time_parity_unverified"),
        "trained_artifact",
        training_anchor="15:00_close_proxy",
        point_in_time_parity=False,
        production_authority=False,
    )
    return LoadedScoringProfile(
        "v1",
        {
            head.strategy: HeadRuntime(
                head.strategy,
                LearnedRankPredictor(head),
                SingleHeadCombiner(),
                replace(evidence, training_label_cutoff=head.split.label_cutoff),
            )
            for head in pair.heads
        },
        tuple(
            ModelPublicationIdentity(
                "v1",
                head.strategy,
                head.content_hash,
                ContentHash((pair.contract_hash, head.split)),
                head.split.label_cutoff,
                pair.training_input_hash,
                pair.content_hash,
            )
            for head in pair.heads
        ),
    )


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("V1 model requires an object")
    return cast(Mapping[str, object], value)


def _text(values: Mapping[str, object], key: str) -> str:
    value = values[key]
    if not IsNonEmptyText(value):
        raise ValueError("V1 model requires text")
    return value


def _number(values: Mapping[str, object], key: str) -> float:
    value = values[key]
    if not IsFiniteNumber(value):
        raise ValueError("V1 model requires a finite number")
    return float(value)


def _integer(values: Mapping[str, object], key: str) -> int:
    value = values[key]
    if not IsInteger(value):
        raise ValueError("V1 model requires an integer")
    return value


def _vector(values: Mapping[str, object], key: str) -> tuple[float, ...]:
    value = values[key]
    if not isinstance(value, list) or not all(IsFiniteNumber(item) for item in value):
        raise ValueError("V1 model requires a finite vector")
    return tuple(float(item) for item in value)


def _split(raw: object) -> LearnedRankDateSplit:
    values = _object(raw)
    RequireExactKeys(
        values,
        {
            "fitting",
            "early_stopping",
            "calibration",
            "validation",
            "label_cutoff",
            "feature_cutoff",
            "maturity_sessions",
            "embargo_sessions",
        },
        "learned_rank_split",
    )
    partitions: list[tuple[date, ...]] = []
    for key in ("fitting", "early_stopping", "calibration", "validation"):
        dates = values[key]
        if not IsTextSequence(dates):
            raise ValueError("learned-rank split dates are invalid")
        partitions.append(tuple(date.fromisoformat(day) for day in dates))
    return LearnedRankDateSplit(
        partitions[0],
        partitions[1],
        partitions[2],
        partitions[3],
        date.fromisoformat(_text(values, "label_cutoff")),
        date.fromisoformat(_text(values, "feature_cutoff")),
        _integer(values, "maturity_sessions"),
        _integer(values, "embargo_sessions"),
    )


def _head(raw: object) -> LearnedRankHead:
    values = _object(raw)
    RequireExactKeys(
        values,
        {
            "strategy",
            "training_contract_hash",
            "split",
            "transformer_means",
            "transformer_scales",
            "ridge_coefficients",
            "ridge_intercept",
            "calibration_intercept",
            "calibration_slope",
            "rank_model",
            "rank_iterations",
            "training_rows",
            "early_stopping_rows",
            "calibration_rows",
            "industries",
            "validation",
        },
        "learned_rank_head",
    )
    industries = values["industries"]
    if not IsTextSequence(industries):
        raise ValueError("learned-rank coverage is invalid")
    metrics = _object(values["validation"])
    counts = ("source_rows", "predicted_rows", "covered_codes", "covered_dates", "covered_industries")
    measures = ("mean_absolute_error", "root_mean_squared_error", "mean_daily_rank_ic", "mean_daily_ndcg_at_6")
    RequireExactKeys(metrics, {*counts, *measures}, "learned_rank_validation")
    validation = LearnedRankPredictionMetrics(
        *(_integer(metrics, key) for key in counts), *(_number(metrics, key) for key in measures)
    )
    return LearnedRankHead(
        Strategy(_text(values, "strategy")),
        _text(values, "training_contract_hash"),
        _split(values["split"]),
        _vector(values, "transformer_means"),
        _vector(values, "transformer_scales"),
        _vector(values, "ridge_coefficients"),
        _number(values, "ridge_intercept"),
        _number(values, "calibration_intercept"),
        _number(values, "calibration_slope"),
        _text(values, "rank_model"),
        _integer(values, "rank_iterations"),
        _integer(values, "training_rows"),
        _integer(values, "early_stopping_rows"),
        _integer(values, "calibration_rows"),
        tuple(industries),
        validation,
    )


__all__ = ["LearnedRankPredictor", "LoadV1ScoringProfile", "PublishLearnedRankPair", "ReadLearnedRankPair"]

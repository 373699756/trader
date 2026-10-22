"""Strict, atomic Shadow pair codec and batched already-loaded inference."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import lightgbm as lgb
import numpy as np
from lightgbm.basic import LightGBMError

from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.process_lock import ProcessLock
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
from trader.training.infra.profile.composition import SingleHeadCombiner
from trader.training.infra.profile.v1.settings import ParseV1TrainingSettings

_MAXIMUM_ARTIFACT_BYTES = 32 * 1024 * 1024


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
    """One replaced JSON contains both heads; a failed training never exposes half a pair."""
    for head in pair.heads:
        LearnedRankPredictor(head)
    payload = {
        "schema_version": "learned_rank_pair",
        "profile": "v1",
        "score_model_id": "learned_rank",
        "score_scale": LEARNED_RANK_SCORE_SCALE,
        "contract_hash": pair.contract_hash,
        "production_authority": False,
        "prediction_return_validation_passed": False,
        "point_in_time_parity": False,
        "feature_anchor": "previous_completed_session",
        "label_anchor": "signal_close_proxy",
        "content_hash": pair.content_hash,
        "pair": CanonicalValue(pair),
    }
    if len(json.dumps(payload).encode("utf-8")) > _MAXIMUM_ARTIFACT_BYTES:
        raise ValueError("learned-rank artifact exceeds its size limit")
    with ProcessLock(model_root / ".learned_rank.publication.lock", label="learned-rank publication"):
        if (model_root / "learned_rank").is_symlink() or (model_root / "learned_rank/model.json").is_symlink():
            raise ValueError("learned-rank artifact cannot replace a linked path")
        AtomicWriteJson(model_root / "learned_rank" / "model.json", payload)


def ReadLearnedRankPair(model_root: Path) -> LearnedRankPair:
    path = model_root / "learned_rank" / "model.json"
    if not path.parent.exists():
        raise FileNotFoundError(path)
    with ProcessLock(model_root / ".learned_rank.publication.lock", label="learned-rank publication"):
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > _MAXIMUM_ARTIFACT_BYTES:
            raise ValueError("learned-rank artifact path or size is invalid")
        raw = _object(json.loads(path.read_text(encoding="utf-8")))
    RequireExactKeys(
        raw,
        {
            "schema_version",
            "profile",
            "score_model_id",
            "score_scale",
            "contract_hash",
            "production_authority",
            "prediction_return_validation_passed",
            "point_in_time_parity",
            "feature_anchor",
            "label_anchor",
            "content_hash",
            "pair",
        },
        "learned_rank_artifact",
    )
    expected: Mapping[str, object] = {
        "schema_version": "learned_rank_pair",
        "profile": "v1",
        "score_model_id": "learned_rank",
        "score_scale": LEARNED_RANK_SCORE_SCALE,
        "production_authority": False,
        "prediction_return_validation_passed": False,
        "point_in_time_parity": False,
        "feature_anchor": "previous_completed_session",
        "label_anchor": "signal_close_proxy",
    }
    if any(type(raw[key]) is not type(value) or raw[key] != value for key, value in expected.items()):
        raise ValueError("learned-rank artifact identity is invalid")
    values = _object(raw["pair"])
    RequireExactKeys(
        values,
        {"training_input_hash", "source_identity_hash", "calendar_hash", "source_cutoff", "settings", "heads"},
        "learned_rank_pair",
    )
    heads = values["heads"]
    if not isinstance(heads, list) or len(heads) != 2:
        raise ValueError("learned-rank artifact requires both heads")
    pair = LearnedRankPair(
        _text(values, "training_input_hash"),
        _text(values, "source_identity_hash"),
        _text(values, "calendar_hash"),
        date.fromisoformat(_text(values, "source_cutoff")),
        ParseV1TrainingSettings(_object(values["settings"])),
        (_head(heads[0]), _head(heads[1])),
    )
    if raw["content_hash"] != pair.content_hash or raw["contract_hash"] != pair.contract_hash:
        raise ValueError("learned-rank artifact content or contract hash differs")
    return pair


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
        raise ValueError("learned-rank artifact requires an object")
    return cast(Mapping[str, object], value)


def _text(values: Mapping[str, object], key: str) -> str:
    value = values[key]
    if not IsNonEmptyText(value):
        raise ValueError("learned-rank artifact requires text")
    return value


def _number(values: Mapping[str, object], key: str) -> float:
    value = values[key]
    if not IsFiniteNumber(value):
        raise ValueError("learned-rank artifact requires a finite number")
    return float(value)


def _integer(values: Mapping[str, object], key: str) -> int:
    value = values[key]
    if not IsInteger(value):
        raise ValueError("learned-rank artifact requires an integer")
    return value


def _vector(values: Mapping[str, object], key: str) -> tuple[float, ...]:
    value = values[key]
    if not isinstance(value, list) or not all(IsFiniteNumber(item) for item in value):
        raise ValueError("learned-rank artifact requires a finite vector")
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

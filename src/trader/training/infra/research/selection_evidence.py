"""Strict external plan/JSONL decoding for isolated full-population selection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.infra.settings.parser import Boolean, Integer, Number, RequireExactKeys, RequireMapping, Text
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.selection_experiment import (
    SelectionExperimentParentRowIdentity,
    SelectionExperimentPrerequisite,
)
from trader.training.domain.evaluation.historical import ResearchBoard
from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeDateSplit
from trader.training.domain.evaluation.selection_experiment import (
    PairedSelectionRow,
    PairedSelectionSpecification,
    SelectionEvidenceTarget,
    SelectionStrategy,
    SelectionTargetEvidence,
)

_SPLIT_KEYS = (
    "training_dates",
    "early_stopping_dates",
    "calibration_dates",
    "development_confirmation_embargo_dates",
    "confirmation_dates",
    "confirmation_holdout_embargo_dates",
    "terminal_holdout_dates",
)
_Partition = Literal["calibration", "confirmation"]
_DatasetStatus = Literal["historical_point_in_time_parity", "historical_data_insufficient"]
_RecallStatus = Literal["candidate_recall_attributed", "historical_data_insufficient"]


class SelectionEvidenceReader:
    """Never open outcome rows until the application's prerequisite gate passes."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._rows_hash: str | None = None

    def load_plan(self) -> tuple[PairedSelectionSpecification, SelectionExperimentPrerequisite]:
        with (self._root / "plan.json").open("rb") as handle:
            encoded = handle.read(512 * 1024 * 1024 + 1)
        if len(encoded) > 512 * 1024 * 1024:
            raise ValueError("selection plan exceeds its size limit")
        raw = _object(json.loads(encoded))
        RequireExactKeys(raw, {"specification", "prerequisite", "rows_sha256"}, "selection_plan")
        rows_hash = Text(raw, "rows_sha256")
        if re.fullmatch(r"[0-9a-f]{64}", rows_hash) is None:
            raise ValueError("selection row file digest is invalid")
        specification = _specification(RequireMapping(raw, "specification"))
        prerequisite = _prerequisite(RequireMapping(raw, "prerequisite"))
        self._rows_hash = rows_hash
        return specification, prerequisite

    def load_rows(
        self, specification: PairedSelectionSpecification, prerequisite: SelectionExperimentPrerequisite
    ) -> tuple[PairedSelectionRow, ...]:
        if not prerequisite.ready or prerequisite.specification_hash != specification.content_hash:
            raise ValueError("selection rows cannot be read before the sealed prerequisite")
        digest = hashlib.sha256()
        rows: list[PairedSelectionRow] = []
        size = 0
        with (self._root / "rows.jsonl").open("rb") as handle:
            while encoded := handle.readline(16_385):
                size += len(encoded)
                if len(encoded) > 16_384 or size > 4 * 1024**3 or len(rows) >= 2_000_000:
                    raise ValueError("selection evidence exceeds its bounded row or size limit")
                digest.update(encoded)
                rows.append(_row(_object(json.loads(encoded))))
        if digest.hexdigest() != self._rows_hash:
            raise ValueError("selection row file digest does not match its sealed plan")
        return tuple(rows)


def _specification(raw: Mapping[str, object]) -> PairedSelectionSpecification:
    RequireExactKeys(
        raw,
        {
            "strategy",
            "dataset_hash",
            "dataset_manifest_hash",
            "candidate_recall_hash",
            "industry_model_id",
            "industry_model_hash",
            "pooled_ridge_model_id",
            "pooled_ridge_model_hash",
            "pooled_lightgbm_model_id",
            "pooled_lightgbm_model_hash",
            "research_dates",
            "predefined_exit_identity",
            "profile_id",
            "date_split",
            "partition",
            "configuration_hash",
            "cost_reference_hash",
            "bootstrap_master_seed",
        },
        "selection_specification",
    )
    split = RequireMapping(raw, "date_split")
    RequireExactKeys(split, set(_SPLIT_KEYS), "selection_date_split")
    return PairedSelectionSpecification(
        strategy=cast(SelectionStrategy, Text(raw, "strategy")),
        dataset_hash=Text(raw, "dataset_hash"),
        dataset_manifest_hash=Text(raw, "dataset_manifest_hash"),
        candidate_recall_hash=Text(raw, "candidate_recall_hash"),
        industry_model_id=Text(raw, "industry_model_id"),
        industry_model_hash=Text(raw, "industry_model_hash"),
        pooled_ridge_model_id=Text(raw, "pooled_ridge_model_id"),
        pooled_ridge_model_hash=Text(raw, "pooled_ridge_model_hash"),
        pooled_lightgbm_model_id=Text(raw, "pooled_lightgbm_model_id"),
        pooled_lightgbm_model_hash=Text(raw, "pooled_lightgbm_model_hash"),
        research_dates=_dates(raw, "research_dates"),
        predefined_exit_identity=Text(raw, "predefined_exit_identity"),
        profile_id=cast(ScoringProfileId, Text(raw, "profile_id")),
        date_split=PointInTimeDateSplit(
            training_dates=_dates(split, "training_dates"),
            early_stopping_dates=_dates(split, "early_stopping_dates"),
            calibration_dates=_dates(split, "calibration_dates"),
            development_confirmation_embargo_dates=_dates(split, "development_confirmation_embargo_dates"),
            confirmation_dates=_dates(split, "confirmation_dates"),
            confirmation_holdout_embargo_dates=_dates(split, "confirmation_holdout_embargo_dates"),
            terminal_holdout_dates=_dates(split, "terminal_holdout_dates"),
        ),
        partition=cast(_Partition, Text(raw, "partition")),
        configuration_hash=Text(raw, "configuration_hash"),
        cost_reference_hash=Text(raw, "cost_reference_hash"),
        bootstrap_master_seed=Integer(raw, "bootstrap_master_seed", minimum=1),
    )


def _prerequisite(raw: Mapping[str, object]) -> SelectionExperimentPrerequisite:
    RequireExactKeys(
        raw,
        {
            "specification_hash",
            "qualification_hash",
            "dataset_status",
            "candidate_recall_status",
            "dataset_hash",
            "dataset_manifest_hash",
            "candidate_recall_hash",
            "point_in_time_parity",
            "terminal_holdout_opened",
            "population_rows",
            "failure_reasons",
        },
        "selection_prerequisite",
    )
    parents: list[SelectionExperimentParentRowIdentity] = []
    for value in _list(raw, "population_rows"):
        item = _object(value)
        RequireExactKeys(item, {"trade_date", "code", "parent_row_hash"}, "selection_parent")
        parents.append(
            SelectionExperimentParentRowIdentity(
                date.fromisoformat(Text(item, "trade_date")),
                Text(item, "code"),
                Text(item, "parent_row_hash"),
            )
        )
    return SelectionExperimentPrerequisite(
        specification_hash=Text(raw, "specification_hash"),
        qualification_hash=Text(raw, "qualification_hash"),
        dataset_status=cast(_DatasetStatus, Text(raw, "dataset_status")),
        candidate_recall_status=cast(_RecallStatus, Text(raw, "candidate_recall_status")),
        dataset_hash=Text(raw, "dataset_hash"),
        dataset_manifest_hash=Text(raw, "dataset_manifest_hash"),
        candidate_recall_hash=Text(raw, "candidate_recall_hash"),
        point_in_time_parity=Boolean(raw, "point_in_time_parity"),
        terminal_holdout_opened=Boolean(raw, "terminal_holdout_opened"),
        population_rows=tuple(parents),
        failure_reasons=_texts(raw, "failure_reasons"),
    )


def _row(raw: Mapping[str, object]) -> PairedSelectionRow:
    RequireExactKeys(
        raw,
        {
            "trade_date",
            "code",
            "board",
            "industry",
            "parent_row_hash",
            "rule_score",
            "estimated_round_trip_cost",
            "risk_eligible",
            "action_eligible",
            "capacity_sufficient",
            "execution_capacity",
            "severe_loss",
            "mae_atr20",
            "target_evidence",
            "point_in_time_parity",
        },
        "selection_row",
    )
    targets: list[SelectionTargetEvidence] = []
    for value in _list(raw, "target_evidence"):
        item = _object(value)
        RequireExactKeys(
            item,
            {
                "target",
                "industry_prediction_pre_cost",
                "pooled_ridge_prediction_pre_cost",
                "pooled_lightgbm_prediction_pre_cost",
                "actual_pre_cost_excess_return",
                "exit_untradable",
            },
            "selection_target",
        )
        targets.append(
            SelectionTargetEvidence(
                cast(SelectionEvidenceTarget, Text(item, "target")),
                Number(item, "industry_prediction_pre_cost"),
                Number(item, "pooled_ridge_prediction_pre_cost"),
                Number(item, "pooled_lightgbm_prediction_pre_cost"),
                Number(item, "actual_pre_cost_excess_return"),
                Boolean(item, "exit_untradable"),
            )
        )
    return PairedSelectionRow(
        trade_date=date.fromisoformat(Text(raw, "trade_date")),
        code=Text(raw, "code"),
        board=cast(ResearchBoard, Text(raw, "board")),
        industry=Text(raw, "industry"),
        parent_row_hash=Text(raw, "parent_row_hash"),
        rule_score=Number(raw, "rule_score"),
        estimated_round_trip_cost=Number(raw, "estimated_round_trip_cost"),
        risk_eligible=Boolean(raw, "risk_eligible"),
        action_eligible=Boolean(raw, "action_eligible"),
        capacity_sufficient=Boolean(raw, "capacity_sufficient"),
        execution_capacity=Number(raw, "execution_capacity"),
        severe_loss=Boolean(raw, "severe_loss"),
        mae_atr20=Number(raw, "mae_atr20"),
        target_evidence=tuple(targets),
        point_in_time_parity=Boolean(raw, "point_in_time_parity"),
    )


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("selection evidence must contain JSON objects")
    return value


def _list(raw: Mapping[str, object], name: str) -> list[object]:
    value = raw.get(name)
    if not isinstance(value, list):
        raise ValueError("selection evidence requires an array")
    return value


def _texts(raw: Mapping[str, object], name: str) -> tuple[str, ...]:
    values = _list(raw, name)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("selection evidence requires text array members")
    return tuple(cast(str, value) for value in values)


def _dates(raw: Mapping[str, object], name: str) -> tuple[date, ...]:
    return tuple(date.fromisoformat(value) for value in _texts(raw, name))


__all__ = ["SelectionEvidenceReader"]

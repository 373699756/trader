"""Conflict-safe sharded JSON artifacts for point-in-time research datasets."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.domain.market.feature_contracts import FeatureVector
from trader.domain.market.models import Board
from trader.domain.outcome.models import (
    OutcomeExitStatus,
    RecommendationOutcome,
)
from trader.domain.recommendation.models import Strategy
from trader.domain.research.point_in_time_dataset import (
    PointInTimeBoardPopulation,
    PointInTimeBoundaryCount,
    PointInTimeCostOutcome,
    PointInTimeCoverage,
    PointInTimeDatasetManifest,
    PointInTimeDatasetReport,
    PointInTimeDatasetRow,
    PointInTimeDatasetState,
    PointInTimeDateSplit,
    PointInTimeDayDataset,
    PointInTimeEventFact,
    PointInTimeIndustryFact,
    PointInTimePartitionManifest,
    PointInTimePartitionName,
    PointInTimeRejectionBoundary,
    PointInTimeSourceIdentity,
)


class PointInTimeDatasetArtifactConflictError(RuntimeError):
    """Raised when a dataset artifact is missing, conflicting, or corrupt."""


class PointInTimeDatasetArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, report: PointInTimeDatasetReport) -> PointInTimeDatasetReport:
        root_path = self._root / "point-in-time-dataset.json"
        if root_path.exists():
            existing = self.verify()
            if existing.content_hash != report.content_hash:
                raise PointInTimeDatasetArtifactConflictError("point-in-time dataset artifact identity conflict")
            return existing
        self._root.mkdir(parents=True, exist_ok=True)
        days_root = self._root / "days"
        days_root.mkdir(exist_ok=True)
        day_entries: list[dict[str, object]] = []
        for day in report.days:
            relative = f"days/{day.trade_date.isoformat()}.json"
            payload = _encode_day(day)
            payload["content_hash"] = day.content_hash
            _write_once(self._root / relative, payload)
            day_entries.append(
                {
                    "trade_date": day.trade_date.isoformat(),
                    "path": relative,
                    "content_hash": day.content_hash,
                }
            )
        root_payload = _encode_report_root(report, day_entries)
        root_payload["artifact_hash"] = canonical_hash(root_payload)
        _write_once(root_path, root_payload)
        verified = self.verify()
        if verified.content_hash != report.content_hash:
            raise PointInTimeDatasetArtifactConflictError("point-in-time dataset artifact identity conflict")
        return verified

    def verify(self) -> PointInTimeDatasetReport:
        try:
            root = _read_object(self._root / "point-in-time-dataset.json")
            artifact_hash = _pop_string(root, "artifact_hash")
            if canonical_hash(root) != artifact_hash:
                raise ValueError("point-in-time dataset root hash mismatch")
            day_entries = root.pop("days")
            if not isinstance(day_entries, list) or not all(isinstance(item, dict) for item in day_entries):
                raise TypeError("point-in-time dataset day index is invalid")
            days = tuple(self._read_day(cast(dict[str, object], item)) for item in day_entries)
            report = _decode_report_root(root, days)
            if report.content_hash != _string(root["content_hash"]):
                raise ValueError("point-in-time dataset reconstructed hash mismatch")
            return report
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PointInTimeDatasetArtifactConflictError(
                "point-in-time dataset artifact schema or hash is invalid"
            ) from exc

    def _read_day(self, entry: dict[str, object]) -> PointInTimeDayDataset:
        if set(entry) != {"trade_date", "path", "content_hash"}:
            raise ValueError("point-in-time dataset day index fields are invalid")
        trade_date = _string(entry["trade_date"])
        relative = _string(entry["path"])
        expected_hash = _string(entry["content_hash"])
        parsed_date = date.fromisoformat(trade_date)
        if parsed_date.isoformat() != trade_date:
            raise ValueError("point-in-time dataset day date is invalid")
        if relative != f"days/{trade_date}.json":
            raise ValueError("point-in-time dataset day path is invalid")
        payload = _read_object(self._root / relative)
        stored_hash = _pop_string(payload, "content_hash")
        if stored_hash != expected_hash:
            raise ValueError("point-in-time dataset day index hash mismatch")
        day = _decode_day(payload)
        if day.trade_date.isoformat() != trade_date or day.content_hash != stored_hash:
            raise ValueError("point-in-time dataset day reconstructed hash mismatch")
        return day


def _write_once(path: Path, payload: dict[str, object]) -> None:
    rendered = canonical_json(payload)
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise PointInTimeDatasetArtifactConflictError("point-in-time dataset shard identity conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != rendered:
                raise PointInTimeDatasetArtifactConflictError("point-in-time dataset shard identity conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def _encode_report_root(
    report: PointInTimeDatasetReport,
    days: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "qualification_hash": report.qualification_hash,
        "state": report.state,
        "days": days,
        "manifest": _encode_manifest(report.manifest) if report.manifest is not None else None,
        "failure_reasons": list(report.failure_reasons),
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "production_authority": report.production_authority,
        "schema_version": report.schema_version,
        "content_hash": report.content_hash,
    }


def _decode_report_root(
    raw: dict[str, object],
    days: tuple[PointInTimeDayDataset, ...],
) -> PointInTimeDatasetReport:
    expected = {
        "qualification_hash",
        "state",
        "manifest",
        "failure_reasons",
        "terminal_holdout_opened",
        "production_authority",
        "schema_version",
        "content_hash",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time dataset root fields are invalid")
    manifest = raw["manifest"]
    if manifest is not None and not isinstance(manifest, dict):
        raise TypeError("point-in-time dataset manifest is invalid")
    return PointInTimeDatasetReport(
        qualification_hash=_string(raw["qualification_hash"]),
        state=cast(PointInTimeDatasetState, _string(raw["state"])),
        days=days,
        manifest=_decode_manifest(cast(dict[str, object], manifest)) if manifest is not None else None,
        failure_reasons=_strings(raw["failure_reasons"]),
        terminal_holdout_opened=_bool(raw["terminal_holdout_opened"]),
        production_authority=_bool(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_manifest(manifest: PointInTimeDatasetManifest) -> dict[str, object]:
    return {
        "qualification_hash": manifest.qualification_hash,
        "daily_archive_manifest_hash": manifest.daily_archive_manifest_hash,
        "feature_manifest_hash": manifest.feature_manifest_hash,
        "calendar_hash": manifest.calendar_hash,
        "security_master_hash": manifest.security_master_hash,
        "selection_policy_hash": manifest.selection_policy_hash,
        "date_split": _encode_date_split(manifest.date_split),
        "date_split_hash": manifest.date_split_hash,
        "partitions": [_encode_partition(item) for item in manifest.partitions],
        "total_rows": manifest.total_rows,
        "terminal_holdout_rows": manifest.terminal_holdout_rows,
        "terminal_holdout_opened": manifest.terminal_holdout_opened,
        "production_authority": manifest.production_authority,
        "schema_version": manifest.schema_version,
    }


def _decode_manifest(raw: dict[str, object]) -> PointInTimeDatasetManifest:
    expected = {
        "qualification_hash",
        "daily_archive_manifest_hash",
        "feature_manifest_hash",
        "calendar_hash",
        "security_master_hash",
        "selection_policy_hash",
        "date_split",
        "date_split_hash",
        "partitions",
        "total_rows",
        "terminal_holdout_rows",
        "terminal_holdout_opened",
        "production_authority",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time dataset manifest fields are invalid")
    partitions = raw["partitions"]
    if not isinstance(partitions, list) or not all(isinstance(item, dict) for item in partitions):
        raise TypeError("point-in-time dataset partitions are invalid")
    date_split = _object(raw["date_split"])
    return PointInTimeDatasetManifest(
        qualification_hash=_string(raw["qualification_hash"]),
        daily_archive_manifest_hash=_string(raw["daily_archive_manifest_hash"]),
        feature_manifest_hash=_string(raw["feature_manifest_hash"]),
        calendar_hash=_string(raw["calendar_hash"]),
        security_master_hash=_string(raw["security_master_hash"]),
        selection_policy_hash=_string(raw["selection_policy_hash"]),
        date_split=_decode_date_split(date_split),
        date_split_hash=_string(raw["date_split_hash"]),
        partitions=tuple(_decode_partition(cast(dict[str, object], item)) for item in partitions),
        total_rows=_int(raw["total_rows"]),
        terminal_holdout_rows=_int(raw["terminal_holdout_rows"]),
        terminal_holdout_opened=_bool(raw["terminal_holdout_opened"]),
        production_authority=_bool(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_partition(item: PointInTimePartitionManifest) -> dict[str, object]:
    return {
        "name": item.name,
        "dates": [value.isoformat() for value in item.dates],
        "day_hashes": list(item.day_hashes),
        "row_count": item.row_count,
    }


def _encode_date_split(item: PointInTimeDateSplit) -> dict[str, object]:
    return {
        "training_dates": _encode_dates(item.training_dates),
        "early_stopping_dates": _encode_dates(item.early_stopping_dates),
        "calibration_dates": _encode_dates(item.calibration_dates),
        "development_confirmation_embargo_dates": _encode_dates(item.development_confirmation_embargo_dates),
        "confirmation_dates": _encode_dates(item.confirmation_dates),
        "confirmation_holdout_embargo_dates": _encode_dates(item.confirmation_holdout_embargo_dates),
        "terminal_holdout_dates": _encode_dates(item.terminal_holdout_dates),
        "terminal_holdout_opened": item.terminal_holdout_opened,
        "production_authority": item.production_authority,
        "schema_version": item.schema_version,
    }


def _decode_date_split(raw: dict[str, object]) -> PointInTimeDateSplit:
    expected = {
        "training_dates",
        "early_stopping_dates",
        "calibration_dates",
        "development_confirmation_embargo_dates",
        "confirmation_dates",
        "confirmation_holdout_embargo_dates",
        "terminal_holdout_dates",
        "terminal_holdout_opened",
        "production_authority",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time date split fields are invalid")
    return PointInTimeDateSplit(
        training_dates=_dates(raw["training_dates"]),
        early_stopping_dates=_dates(raw["early_stopping_dates"]),
        calibration_dates=_dates(raw["calibration_dates"]),
        development_confirmation_embargo_dates=_dates(raw["development_confirmation_embargo_dates"]),
        confirmation_dates=_dates(raw["confirmation_dates"]),
        confirmation_holdout_embargo_dates=_dates(raw["confirmation_holdout_embargo_dates"]),
        terminal_holdout_dates=_dates(raw["terminal_holdout_dates"]),
        terminal_holdout_opened=_bool(raw["terminal_holdout_opened"]),
        production_authority=_bool(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _decode_partition(raw: dict[str, object]) -> PointInTimePartitionManifest:
    if set(raw) != {"name", "dates", "day_hashes", "row_count"}:
        raise ValueError("point-in-time partition fields are invalid")
    return PointInTimePartitionManifest(
        name=cast(PointInTimePartitionName, _string(raw["name"])),
        dates=_dates(raw["dates"]),
        day_hashes=_strings(raw["day_hashes"]),
        row_count=_int(raw["row_count"]),
    )


def _encode_day(day: PointInTimeDayDataset) -> dict[str, object]:
    return {
        "trade_date": day.trade_date.isoformat(),
        "anchor_at": day.anchor_at.isoformat(),
        "rows": [_encode_row(item) for item in day.rows],
        "coverage": _encode_coverage(day.coverage),
        "board_populations": [_encode_board_population(item) for item in day.board_populations],
        "benchmark_identity": day.benchmark_identity,
    }


def _decode_day(raw: dict[str, object]) -> PointInTimeDayDataset:
    if set(raw) != {
        "trade_date",
        "anchor_at",
        "rows",
        "coverage",
        "board_populations",
        "benchmark_identity",
    }:
        raise ValueError("point-in-time day fields are invalid")
    rows = raw["rows"]
    coverage = raw["coverage"]
    populations = _objects(raw["board_populations"])
    if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
        raise TypeError("point-in-time rows are invalid")
    if not isinstance(coverage, dict):
        raise TypeError("point-in-time coverage is invalid")
    return PointInTimeDayDataset(
        trade_date=date.fromisoformat(_string(raw["trade_date"])),
        anchor_at=_shanghai_datetime(raw["anchor_at"]),
        rows=tuple(_decode_row(cast(dict[str, object], item)) for item in rows),
        coverage=_decode_coverage(cast(dict[str, object], coverage)),
        board_populations=tuple(_decode_board_population(item) for item in populations),
        benchmark_identity=_string(raw["benchmark_identity"]),
    )


def _encode_board_population(item: PointInTimeBoardPopulation) -> dict[str, object]:
    return {
        "board": item.board.value,
        "population_version": item.population_version,
        "eligible_rows": item.eligible_rows,
    }


def _decode_board_population(raw: dict[str, object]) -> PointInTimeBoardPopulation:
    if set(raw) != {"board", "population_version", "eligible_rows"}:
        raise ValueError("point-in-time board population fields are invalid")
    return PointInTimeBoardPopulation(
        Board(_string(raw["board"])),
        _string(raw["population_version"]),
        _int(raw["eligible_rows"]),
    )


def _encode_row(row: PointInTimeDatasetRow) -> dict[str, object]:
    return {
        "code": row.code,
        "trade_date": row.trade_date.isoformat(),
        "anchor_at": row.anchor_at.isoformat(),
        "board": row.board.value,
        "industry": row.industry,
        "anchor_raw_price": row.anchor_raw_price,
        "feature_vector": {
            "manifest_hash": row.feature_vector.manifest_hash,
            "values": list(row.feature_vector.values),
            "missing_mask": list(row.feature_vector.missing_mask),
        },
        "source_identity": _encode_source_identity(row.source_identity),
        "industry_fact": _encode_industry_fact(row.industry_fact),
        "event_facts": [_encode_event_fact(item) for item in row.event_facts],
        "first_rejection_boundary": row.first_rejection_boundary,
        "rejection_reasons": list(row.rejection_reasons),
        "benchmark_eligible": row.benchmark_eligible,
        "candidate_eligible": row.candidate_eligible,
        "candidate_score": row.candidate_score,
        "candidate_rank": row.candidate_rank,
        "outcomes": [_encode_cost_outcome(item) for item in row.outcomes],
    }


def _decode_row(raw: dict[str, object]) -> PointInTimeDatasetRow:
    expected = {
        "code",
        "trade_date",
        "anchor_at",
        "board",
        "industry",
        "anchor_raw_price",
        "feature_vector",
        "source_identity",
        "industry_fact",
        "event_facts",
        "first_rejection_boundary",
        "rejection_reasons",
        "benchmark_eligible",
        "candidate_eligible",
        "candidate_score",
        "candidate_rank",
        "outcomes",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time row fields are invalid")
    vector = _object(raw["feature_vector"])
    if set(vector) != {"manifest_hash", "values", "missing_mask"}:
        raise ValueError("point-in-time feature vector fields are invalid")
    identity = _object(raw["source_identity"])
    industry_fact = _object(raw["industry_fact"])
    facts = _objects(raw["event_facts"])
    outcomes = _objects(raw["outcomes"])
    score = raw["candidate_score"]
    return PointInTimeDatasetRow(
        code=_string(raw["code"]),
        trade_date=date.fromisoformat(_string(raw["trade_date"])),
        anchor_at=_shanghai_datetime(raw["anchor_at"]),
        board=Board(_string(raw["board"])),
        industry=_string(raw["industry"]),
        anchor_raw_price=_float(raw["anchor_raw_price"]),
        feature_vector=FeatureVector(
            _string(vector["manifest_hash"]),
            _optional_floats(vector["values"]),
            _bools(vector["missing_mask"]),
        ),
        source_identity=_decode_source_identity(identity),
        industry_fact=_decode_industry_fact(industry_fact),
        event_facts=tuple(_decode_event_fact(item) for item in facts),
        first_rejection_boundary=cast(PointInTimeRejectionBoundary, _string(raw["first_rejection_boundary"])),
        rejection_reasons=_strings(raw["rejection_reasons"]),
        benchmark_eligible=_bool(raw["benchmark_eligible"]),
        candidate_eligible=_bool(raw["candidate_eligible"]),
        candidate_score=None if score is None else _float(score),
        candidate_rank=_int(raw["candidate_rank"]),
        outcomes=tuple(_decode_cost_outcome(item) for item in outcomes),
    )


def _encode_source_identity(item: PointInTimeSourceIdentity) -> dict[str, object]:
    return {
        "daily_path_hash": item.daily_path_hash,
        "minute_path_hash": item.minute_path_hash,
        "security_fact_hash": item.security_fact_hash,
        "industry_fact_hash": item.industry_fact_hash,
        "event_fact_hash": item.event_fact_hash,
    }


def _decode_source_identity(raw: dict[str, object]) -> PointInTimeSourceIdentity:
    expected = {
        "daily_path_hash",
        "minute_path_hash",
        "security_fact_hash",
        "industry_fact_hash",
        "event_fact_hash",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time source identity fields are invalid")
    return PointInTimeSourceIdentity(
        daily_path_hash=_string(raw["daily_path_hash"]),
        minute_path_hash=_string(raw["minute_path_hash"]),
        security_fact_hash=_string(raw["security_fact_hash"]),
        industry_fact_hash=_string(raw["industry_fact_hash"]),
        event_fact_hash=_string(raw["event_fact_hash"]),
    )


def _encode_event_fact(item: PointInTimeEventFact) -> dict[str, object]:
    return {
        "fact_id": item.fact_id,
        "published_at": item.published_at.isoformat(),
        "effective_at": item.effective_at.isoformat(),
        "anchor_at": item.anchor_at.isoformat(),
        "content_hash": item.content_hash,
    }


def _encode_industry_fact(item: PointInTimeIndustryFact) -> dict[str, object]:
    return {
        "code": item.code,
        "industry": item.industry,
        "classification": item.classification,
        "effective_from": item.effective_from.isoformat(),
        "effective_to": item.effective_to.isoformat() if item.effective_to is not None else None,
        "queried_at": item.queried_at.isoformat(),
        "source": item.source,
        "content_hash": item.content_hash,
    }


def _decode_industry_fact(raw: dict[str, object]) -> PointInTimeIndustryFact:
    expected = {
        "code",
        "industry",
        "classification",
        "effective_from",
        "effective_to",
        "queried_at",
        "source",
        "content_hash",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time industry fact fields are invalid")
    effective_to = raw["effective_to"]
    return PointInTimeIndustryFact(
        code=_string(raw["code"]),
        industry=_string(raw["industry"]),
        classification=_string(raw["classification"]),
        effective_from=date.fromisoformat(_string(raw["effective_from"])),
        effective_to=date.fromisoformat(_string(effective_to)) if effective_to is not None else None,
        queried_at=_shanghai_datetime(raw["queried_at"]),
        source=_string(raw["source"]),
        content_hash=_string(raw["content_hash"]),
    )


def _decode_event_fact(raw: dict[str, object]) -> PointInTimeEventFact:
    if set(raw) != {"fact_id", "published_at", "effective_at", "anchor_at", "content_hash"}:
        raise ValueError("point-in-time event fact fields are invalid")
    return PointInTimeEventFact(
        fact_id=_string(raw["fact_id"]),
        published_at=_shanghai_datetime(raw["published_at"]),
        effective_at=_shanghai_datetime(raw["effective_at"]),
        anchor_at=_shanghai_datetime(raw["anchor_at"]),
        content_hash=_string(raw["content_hash"]),
    )


def _encode_cost_outcome(item: PointInTimeCostOutcome) -> dict[str, object]:
    return {"cost_bps": item.cost_bps, "outcome": _encode_outcome(item.outcome)}


def _decode_cost_outcome(raw: dict[str, object]) -> PointInTimeCostOutcome:
    if set(raw) != {"cost_bps", "outcome"}:
        raise ValueError("point-in-time cost outcome fields are invalid")
    return PointInTimeCostOutcome(_int(raw["cost_bps"]), _decode_outcome(_object(raw["outcome"])))


def _encode_outcome(item: RecommendationOutcome) -> dict[str, object]:
    return {
        "snapshot_id": item.snapshot_id,
        "strategy": item.strategy.value,
        "recommend_date": item.recommend_date,
        "stock_code": item.stock_code,
        "horizon": item.horizon,
        "status": item.status,
        "settled_at": item.settled_at.isoformat(),
        "anchor_raw_price": item.anchor_raw_price,
        "anchor_qfq_price": item.anchor_qfq_price,
        "atr20_pct": item.atr20_pct,
        "minimum_qfq_low": item.minimum_qfq_low,
        "end_qfq_close": item.end_qfq_close,
        "exit_status": item.exit_status.value if item.exit_status is not None else None,
        "untradable_dates": list(item.untradable_dates),
        "gross_return_pct": item.gross_return_pct,
        "benchmark_return_pct": item.benchmark_return_pct,
        "net_excess_return_pct": item.net_excess_return_pct,
        "mae_pct": item.mae_pct,
        "mae_atr": item.mae_atr,
        "severe_drawdown": item.severe_drawdown,
        "quality_reason": item.quality_reason,
        "schema_identity": item.schema_identity,
    }


def _decode_outcome(raw: dict[str, object]) -> RecommendationOutcome:
    expected = {
        "snapshot_id",
        "strategy",
        "recommend_date",
        "stock_code",
        "horizon",
        "status",
        "settled_at",
        "anchor_raw_price",
        "anchor_qfq_price",
        "atr20_pct",
        "minimum_qfq_low",
        "end_qfq_close",
        "exit_status",
        "untradable_dates",
        "gross_return_pct",
        "benchmark_return_pct",
        "net_excess_return_pct",
        "mae_pct",
        "mae_atr",
        "severe_drawdown",
        "quality_reason",
        "schema_identity",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time outcome fields are invalid")
    exit_status = raw["exit_status"]
    return RecommendationOutcome(
        snapshot_id=_string(raw["snapshot_id"]),
        strategy=Strategy(_string(raw["strategy"])),
        recommend_date=_string(raw["recommend_date"]),
        stock_code=_string(raw["stock_code"]),
        horizon=_int(raw["horizon"]),
        status=cast(Literal["complete", "benchmark_missing", "insufficient_data"], _string(raw["status"])),
        settled_at=_shanghai_datetime(raw["settled_at"]),
        anchor_raw_price=_float(raw["anchor_raw_price"]),
        anchor_qfq_price=_optional_float(raw["anchor_qfq_price"]),
        atr20_pct=_float(raw["atr20_pct"]),
        minimum_qfq_low=_optional_float(raw["minimum_qfq_low"]),
        end_qfq_close=_optional_float(raw["end_qfq_close"]),
        exit_status=OutcomeExitStatus(_string(exit_status)) if exit_status is not None else None,
        untradable_dates=_strings(raw["untradable_dates"]),
        gross_return_pct=_optional_float(raw["gross_return_pct"]),
        benchmark_return_pct=_optional_float(raw["benchmark_return_pct"]),
        net_excess_return_pct=_optional_float(raw["net_excess_return_pct"]),
        mae_pct=_optional_float(raw["mae_pct"]),
        mae_atr=_optional_float(raw["mae_atr"]),
        severe_drawdown=_optional_bool(raw["severe_drawdown"]),
        quality_reason=_string(raw["quality_reason"]),
        schema_identity=_string(raw["schema_identity"]),
    )


def _encode_coverage(item: PointInTimeCoverage) -> dict[str, object]:
    return {
        "total_rows": item.total_rows,
        "benchmark_eligible_rows": item.benchmark_eligible_rows,
        "candidate_eligible_rows": item.candidate_eligible_rows,
        "label_complete_rows": item.label_complete_rows,
        "boundary_counts": [{"boundary": value.boundary, "count": value.count} for value in item.boundary_counts],
    }


def _decode_coverage(raw: dict[str, object]) -> PointInTimeCoverage:
    expected = {
        "total_rows",
        "benchmark_eligible_rows",
        "candidate_eligible_rows",
        "label_complete_rows",
        "boundary_counts",
    }
    if set(raw) != expected:
        raise ValueError("point-in-time coverage fields are invalid")
    values = _objects(raw["boundary_counts"])
    return PointInTimeCoverage(
        total_rows=_int(raw["total_rows"]),
        benchmark_eligible_rows=_int(raw["benchmark_eligible_rows"]),
        candidate_eligible_rows=_int(raw["candidate_eligible_rows"]),
        label_complete_rows=_int(raw["label_complete_rows"]),
        boundary_counts=tuple(
            PointInTimeBoundaryCount(
                cast(PointInTimeRejectionBoundary, _string(item["boundary"])),
                _int(item["count"]),
            )
            for item in values
        ),
    )


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("point-in-time artifact must be an object")
    return cast(dict[str, object], value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("point-in-time artifact value must be an object")
    return cast(dict[str, object], value)


def _objects(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("point-in-time artifact value must be an object list")
    return tuple(cast(dict[str, object], item) for item in value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("point-in-time artifact value must be a string")
    return value


def _pop_string(value: dict[str, object], key: str) -> str:
    return _string(value.pop(key))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("point-in-time artifact value must be a string list")
    return tuple(value)


def _dates(value: object) -> tuple[date, ...]:
    return tuple(date.fromisoformat(item) for item in _strings(value))


def _encode_dates(value: tuple[date, ...]) -> list[str]:
    return [item.isoformat() for item in value]


def _shanghai_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(_string(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("point-in-time artifact datetime must be timezone-aware")
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("point-in-time artifact value must be a boolean")
    return value


def _optional_bool(value: object) -> bool | None:
    return None if value is None else _bool(value)


def _bools(value: object) -> tuple[bool, ...]:
    if not isinstance(value, list):
        raise TypeError("point-in-time artifact value must be a boolean list")
    return tuple(_bool(item) for item in value)


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("point-in-time artifact value must be an integer")
    return value


def _float(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("point-in-time artifact value must be a number")
    return float(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else _float(value)


def _optional_floats(value: object) -> tuple[float | None, ...]:
    if not isinstance(value, list):
        raise TypeError("point-in-time artifact value must be a number list")
    return tuple(_optional_float(item) for item in value)


__all__ = [
    "PointInTimeDatasetArtifactConflictError",
    "PointInTimeDatasetArtifactStore",
]

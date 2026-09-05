"""Immutable Polars partitions and reproducible manifests for Score-R2."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType

import polars as pl

from trader.application.research.models import HistoricalExtractedDay, ScoreR2HistoricalExtraction

_SCHEMA_VERSION = "score_r2_partition"
_MANIFEST_NAME = "manifest.json"


class HistoricalPartitionConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalPartitionFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class HistoricalPartitionManifest:
    trade_date: date
    day_hash: str
    files: tuple[HistoricalPartitionFile, ...]
    content_hash: str
    schema_version: str = _SCHEMA_VERSION


class PolarsHistoricalPartitionStore:
    """Write each date once; verified identical replays are idempotent."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def write_extraction(self, extraction: ScoreR2HistoricalExtraction) -> tuple[HistoricalPartitionManifest, ...]:
        self._root.mkdir(parents=True, exist_ok=True)
        top_manifest_path = self._root / "extraction-manifest.json"
        if top_manifest_path.exists():
            existing = self.verify_extraction()
            if existing.get("extraction_hash") != extraction.content_hash:
                raise HistoricalPartitionConflictError("Score-R2 top manifest identity conflict")
        manifests = tuple(self.write_day(day) for day in extraction.days)
        top_payload: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "extraction_schema_version": extraction.schema_version,
            "extraction_hash": extraction.content_hash,
            "research_identity": extraction.research_identity,
            "research_spec_hash": extraction.research_spec_hash,
            "status": extraction.status,
            "coverage": [_canonical_value(item) for item in extraction.coverage],
            "days": [
                {
                    "trade_date": manifest.trade_date.isoformat(),
                    "day_hash": manifest.day_hash,
                    "manifest_hash": manifest.content_hash,
                }
                for manifest in manifests
            ],
        }
        top_payload["content_hash"] = _payload_hash(top_payload)
        _write_immutable_json(top_manifest_path, top_payload)
        return manifests

    def verify_extraction(self) -> dict[str, object]:
        path = self._root / "extraction-manifest.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HistoricalPartitionConflictError("Score-R2 top manifest is invalid") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise HistoricalPartitionConflictError("Score-R2 top manifest schema mismatch")
        content_hash = raw.get("content_hash")
        identity = {key: value for key, value in raw.items() if key != "content_hash"}
        if not isinstance(content_hash, str) or _payload_hash(identity) != content_hash:
            raise HistoricalPartitionConflictError("Score-R2 top manifest hash mismatch")
        days = raw.get("days")
        if not isinstance(days, list):
            raise HistoricalPartitionConflictError("Score-R2 top manifest days are invalid")
        for item in days:
            if not isinstance(item, dict):
                raise HistoricalPartitionConflictError("Score-R2 top manifest day identity is invalid")
            trade_date = date.fromisoformat(str(item.get("trade_date")))
            manifest = self.verify_day(trade_date)
            if manifest.day_hash != item.get("day_hash") or manifest.content_hash != item.get("manifest_hash"):
                raise HistoricalPartitionConflictError("Score-R2 top manifest day mismatch")
        return {str(key): value for key, value in raw.items()}

    def write_day(self, day: HistoricalExtractedDay) -> HistoricalPartitionManifest:
        target = self._root / day.summary.trade_date.isoformat()
        if target.exists():
            existing = self.verify_day(day.summary.trade_date)
            if existing.day_hash != day.content_hash:
                raise HistoricalPartitionConflictError("Score-R2 day partition identity conflict")
            return existing
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=self._root))
        try:
            self._write_tables(temporary, day)
            manifest = _build_manifest(temporary, day)
            _write_json(temporary / _MANIFEST_NAME, _manifest_payload(manifest))
            try:
                os.replace(temporary, target)
            except OSError:
                if not target.exists():
                    raise
                existing = self.verify_day(day.summary.trade_date)
                if existing.day_hash != day.content_hash:
                    raise HistoricalPartitionConflictError("Score-R2 day partition identity conflict") from None
                return existing
            return self.verify_day(day.summary.trade_date)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def verify_day(self, trade_date: date) -> HistoricalPartitionManifest:
        directory = self._root / trade_date.isoformat()
        try:
            raw = json.loads((directory / _MANIFEST_NAME).read_text(encoding="utf-8"))
            manifest = _manifest_from_payload(raw)
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoricalPartitionConflictError("Score-R2 partition manifest is invalid") from exc
        if (
            manifest.trade_date != trade_date
            or _payload_hash(_manifest_identity_payload(manifest)) != manifest.content_hash
        ):
            raise HistoricalPartitionConflictError("Score-R2 partition manifest identity mismatch")
        for item in manifest.files:
            path = directory / item.path
            if not path.is_file() or path.stat().st_size != item.size or _file_hash(path) != item.sha256:
                raise HistoricalPartitionConflictError("Score-R2 partition file verification failed")
        return manifest

    @staticmethod
    def _write_tables(directory: Path, day: HistoricalExtractedDay) -> None:
        tables = {
            "day_identity.parquet": (_day_identity(day),),
            "board_coverage.parquet": day.summary.board_coverages,
            "hard_filter_aggregates.parquet": day.summary.hard_filter_aggregates,
            "candidates.parquet": day.summary.candidates,
            "full_candidates.parquet": day.full_fields.candidates,
            "evaluated_candidates.parquet": day.evaluated,
            "daily_bars.parquet": day.full_fields.daily_bars,
            "minute_bars.parquet": day.full_fields.minute_bars,
            "adjustment_windows.parquet": day.full_fields.adjustment_windows,
            "settlements.parquet": day.full_fields.settlements,
            "proofs.parquet": day.proofs,
        }
        for name, records in tables.items():
            rows = [{"payload": _canonical_json(record)} for record in records]
            pl.DataFrame(rows, schema={"payload": pl.String}).write_parquet(
                directory / name,
                compression="zstd",
                statistics=True,
            )


def _build_manifest(directory: Path, day: HistoricalExtractedDay) -> HistoricalPartitionManifest:
    files = tuple(
        HistoricalPartitionFile(path.name, path.stat().st_size, _file_hash(path))
        for path in sorted(directory.glob("*.parquet"), key=lambda value: value.name)
    )
    identity: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "trade_date": day.summary.trade_date.isoformat(),
        "day_hash": day.content_hash,
        "files": [_canonical_value(item) for item in files],
    }
    return HistoricalPartitionManifest(day.summary.trade_date, day.content_hash, files, _payload_hash(identity))


def _day_identity(day: HistoricalExtractedDay) -> dict[str, object]:
    summary = day.summary
    return {
        "trade_date": summary.trade_date,
        "observed_at": summary.observed_at,
        "daily_feature_pack_version": summary.daily_feature_pack_version,
        "market_epoch_version": summary.market_epoch_version,
        "candidate_quote_epoch_version": summary.candidate_quote_epoch_version,
        "research_epoch_version": summary.research_epoch_version,
        "input_hash": summary.input_hash,
        "config_version": summary.config_version,
        "calendar_version": summary.calendar_version,
        "rule_versions": summary.rule_versions,
        "source_versions": summary.source_versions,
        "day_hash": day.content_hash,
    }


def _manifest_payload(manifest: HistoricalPartitionManifest) -> dict[str, object]:
    return {**_manifest_identity_payload(manifest), "content_hash": manifest.content_hash}


def _manifest_identity_payload(manifest: HistoricalPartitionManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "trade_date": manifest.trade_date.isoformat(),
        "day_hash": manifest.day_hash,
        "files": [_canonical_value(item) for item in manifest.files],
    }


def _manifest_from_payload(raw: object) -> HistoricalPartitionManifest:
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("Score-R2 partition schema mismatch")
    files_raw = raw["files"]
    if not isinstance(files_raw, list):
        raise TypeError("Score-R2 partition files must be a list")
    files = tuple(
        HistoricalPartitionFile(str(item["path"]), int(item["size"]), str(item["sha256"]))
        for item in files_raw
        if isinstance(item, dict)
    )
    if len(files) != len(files_raw) or tuple(item.path for item in files) != tuple(sorted(item.path for item in files)):
        raise ValueError("Score-R2 partition files are invalid")
    if any(Path(item.path).name != item.path or item.size < 0 or len(item.sha256) != 64 for item in files):
        raise ValueError("Score-R2 partition file identity is invalid")
    return HistoricalPartitionManifest(
        date.fromisoformat(str(raw["trade_date"])),
        str(raw["day_hash"]),
        files,
        str(raw["content_hash"]),
    )


def _write_immutable_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HistoricalPartitionConflictError("Score-R2 top manifest is invalid") from exc
        if existing != payload:
            raise HistoricalPartitionConflictError("Score-R2 top manifest identity conflict")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    _write_json(temporary, payload)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != payload:
                raise HistoricalPartitionConflictError("Score-R2 top manifest identity conflict") from None
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(_canonical_json(payload), encoding="utf-8")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value), ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _canonical_value(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _canonical_value(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, MappingProxyType):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    return value


def _payload_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "HistoricalPartitionConflictError",
    "HistoricalPartitionManifest",
    "PolarsHistoricalPartitionStore",
]

"""Partitioned BaoStock artifact store."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from trader.domain.research.baostock_daily import (
    BaoStockDailyCell,
    BaoStockDailyManifest,
    BaoStockDailySpec,
    BaoStockTrainingRow,
    build_baostock_coverage_audit,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.tomorrow_v3_input_compatibility import FrozenDailyInputDescriptor
from trader.infra.research.baostock_daily import (
    _FROZEN_DAILY_FIELDS,
    BaoStockDailyArtifactConflictError,
    SQLiteBaoStockDailyShard,
    _checkpoint_database,
    _common_context,
    _decode_cell,
    _decode_spec,
    _file_sha256,
    _json_object,
    _manifest_spec,
    _merged_batches,
    _partition_ref,
    _write_catalog,
    _write_immutable_json,
)
from trader.infra.research.baostock_daily_serialization import _decode_manifest, _encode_manifest


class BaoStockDailyPartitionedArchive:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._catalog = root / "catalog.sqlite3"
        self._manifest = root / "manifest.json"

    def write(
        self,
        spec: BaoStockDailySpec,
        shards: tuple[SQLiteBaoStockDailyShard, ...],
    ) -> BaoStockDailyManifest:
        if not shards:
            raise ValueError("BaoStock partition manifest requires at least one shard")
        snapshots = tuple(shard.snapshot(spec) for shard in shards)
        context = _common_context(spec, snapshots)
        batches = _merged_batches(snapshots)
        audit = build_baostock_coverage_audit(spec, context.calendar, context.universe, batches)
        self._root.mkdir(parents=True, exist_ok=True)
        refs = tuple(
            sorted(
                (_partition_ref(self._root, spec, shard, snapshot) for shard, snapshot in zip(shards, snapshots, strict=True)),
                key=lambda item: item.relative_path,
            )
        )
        if frozenset(code for item in refs for code in item.codes) != frozenset(item.code for item in context.universe):
            raise BaoStockDailyArtifactConflictError("BaoStock partitions do not cover the frozen universe")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".baostock-catalog.", suffix=".sqlite3", dir=self._root)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            _write_catalog(temporary, refs, context.universe, batches)
            _checkpoint_database(temporary)
            catalog_hash = _file_sha256(temporary)
            logical_hash = canonical_hash(tuple((item.relative_path, item.logical_records_hash) for item in refs))
            manifest = BaoStockDailyManifest(
                spec_hash=spec.content_hash,
                calendar_hash=context.calendar.content_hash,
                universe_hash=canonical_hash(context.universe),
                logical_records_hash=logical_hash,
                source_versions_hash=context.source_versions.content_hash,
                source_versions=context.source_versions,
                catalog_sha256=catalog_hash,
                partitions=refs,
                audit=audit,
            )
            if self._manifest.exists() or self._catalog.exists():
                existing = self.verify()
                if existing.content_hash != manifest.content_hash:
                    raise BaoStockDailyArtifactConflictError("BaoStock partitioned artifact identity conflict")
                return existing
            os.link(temporary, self._catalog)
            _write_immutable_json(self._manifest, _encode_manifest(manifest), manifest.content_hash)
            return self.verify()
        finally:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)

    def verify(self) -> BaoStockDailyManifest:
        try:
            raw = _json_object(self._manifest.read_text(encoding="utf-8"))
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str):
                raise TypeError("BaoStock manifest hash is invalid")
            manifest = _decode_manifest(raw)
            if manifest.content_hash != stored_hash or manifest.catalog_sha256 != _file_sha256(self._catalog):
                raise ValueError("BaoStock manifest or catalog hash mismatch")
            for reference in manifest.partitions:
                path = self._root / reference.relative_path
                if _file_sha256(path) != reference.database_sha256:
                    raise ValueError(f"BaoStock partition hash mismatch: {reference.relative_path}")
            return manifest
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock partition manifest is invalid") from exc

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor:
        manifest = self.verify()
        try:
            first = self._root / manifest.partitions[0].relative_path
            with sqlite3.connect(first) as connection:
                row = connection.execute("SELECT spec_json FROM context WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("BaoStock partition context is missing")
            spec = _decode_spec(_json_object(row[0]))
            if spec.content_hash != manifest.spec_hash:
                raise ValueError("BaoStock partition spec hash mismatch")
        except (TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock partition input description is invalid") from exc
        return FrozenDailyInputDescriptor(
            manifest_hash=manifest.content_hash,
            source_identity=spec.research_identity,
            source_cutoff=spec.source_cutoff,
            requested_sessions=spec.sessions,
            primary_key=("code", "trade_date"),
            fields=_FROZEN_DAILY_FIELDS,
            raw_qfq_layout="same_row",
            row_hash_algorithm="sha256",
            frozen=True,
            production_authority=False,
        )

    def read_cells(self, code: str) -> tuple[BaoStockDailyCell, ...]:
        path = self._partition_path_for_code(code)
        with sqlite3.connect(path) as connection:
            rows = connection.execute(
                "SELECT payload_json, content_hash FROM daily_cells WHERE code=? ORDER BY trade_date", (code,)
            ).fetchall()
        cells = tuple(_decode_cell(_json_object(payload)) for payload, _ in rows)
        if any(cell.content_hash != stored_hash for cell, (_, stored_hash) in zip(cells, rows, strict=True)):
            raise BaoStockDailyArtifactConflictError("BaoStock partition daily cell hash is invalid")
        return cells

    def read_training_rows(
        self,
        code: str,
        *,
        allowed_dates: frozenset[date],
    ) -> tuple[BaoStockTrainingRow, ...]:
        manifest = self.verify()
        spec = _manifest_spec(self._root, manifest)
        return SQLiteBaoStockDailyShard(self._partition_path_for_code(code)).read_training_rows(
            spec,
            code,
            allowed_dates=allowed_dates,
        )

    def complete_dates(self) -> tuple[date, ...]:
        manifest = self.verify()
        if manifest.audit.status != "coverage_ready":
            return ()
        spec = _manifest_spec(self._root, manifest)
        context = SQLiteBaoStockDailyShard(self._root / manifest.partitions[0].relative_path).context(spec)
        if context is None:
            raise BaoStockDailyArtifactConflictError("BaoStock partition context is missing")
        return context.calendar.open_dates

    def _partition_path_for_code(self, code: str) -> Path:
        manifest = self.verify()
        reference = next((item for item in manifest.partitions if code in item.codes), None)
        if reference is None:
            raise ValueError("BaoStock code is outside the partition manifest")
        return self._root / reference.relative_path

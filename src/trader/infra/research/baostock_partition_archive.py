"""Partitioned BaoStock artifact store."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeCoverageEvidence,
    BaoStockCoverageAudit,
    BaoStockDailyCell,
    BaoStockDailyManifest,
    BaoStockDailySpec,
    BaoStockPartitionRef,
    BaoStockTrainingRow,
    build_baostock_coverage_audit,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.domain.research.tomorrow_training_input import FrozenDailyInputDescriptor
from trader.infra.research.baostock_catalog import (
    checkpoint_database,
    file_sha256,
    has_pending_wal,
    manifest_spec,
    partition_ref_from_index,
    write_catalog_from_hashes,
    write_immutable_json,
)
from trader.infra.research.baostock_daily import (
    _FROZEN_DAILY_FIELDS,
    BaoStockDailyArtifactConflictError,
    BaoStockShardContextIdentity,
    BaoStockTrainingCodeIdentity,
    SQLiteBaoStockDailyShard,
    _decode_cell,
    _decode_spec,
    _json_object,
    shared_context_identity,
)
from trader.infra.research.baostock_daily_serialization import _decode_manifest, _encode_manifest

BaoStockTrainingInputScope = Literal["complete_manifest", "partial_checkpoint"]


@dataclass(frozen=True)
class BaoStockTrainingTrainingCodeReference:
    relative_path: str
    identity: BaoStockTrainingCodeIdentity

    def __post_init__(self) -> None:
        path = Path(self.relative_path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".sqlite3":
            raise ValueError("BaoStock training shard reference is invalid")


@dataclass(frozen=True)
class BaoStockTrainingTrainingInputSnapshot:
    input_scope: BaoStockTrainingInputScope
    input_hash: str
    calendar: BaoStockCalendar
    universe_count: int
    completed_code_count: int
    references: tuple[BaoStockTrainingTrainingCodeReference, ...]

    def __post_init__(self) -> None:
        references = tuple(sorted(self.references, key=lambda item: item.identity.code))
        codes = tuple(item.identity.code for item in references)
        if (
            self.input_scope not in ("complete_manifest", "partial_checkpoint")
            or len(self.input_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.input_hash)
            or self.universe_count < 1
            or not 0 < len(codes) <= self.completed_code_count <= self.universe_count
            or len(set(codes)) != len(codes)
        ):
            raise ValueError("BaoStock training training input snapshot is invalid")
        if self.input_scope == "complete_manifest" and len(codes) != self.universe_count:
            raise ValueError("BaoStock complete training input must cover the universe")
        object.__setattr__(self, "references", references)

    @property
    def content_hash(self) -> str:
        return self.input_hash

    @property
    def training_codes(self) -> tuple[str, ...]:
        return tuple(item.identity.code for item in self.references)


class BaoStockTrainingTrainingInputArchive:
    def __init__(
        self,
        root: Path,
        spec: BaoStockDailySpec,
        context_identity: BaoStockShardContextIdentity,
        snapshot: BaoStockTrainingTrainingInputSnapshot,
        shards_by_code: dict[str, SQLiteBaoStockDailyShard],
    ) -> None:
        self._root = root
        self._spec = spec
        self._context_identity = context_identity
        self.snapshot = snapshot
        self._shards_by_code = dict(shards_by_code)
        self._references_by_code = {item.identity.code: item.identity for item in snapshot.references}

    @classmethod
    def open(  # noqa: C901 - archive opening validates every identity boundary before exposing the snapshot
        cls,
        root: Path,
        *,
        sessions: int = 2000,
        allow_partial_history: bool = False,
    ) -> BaoStockTrainingTrainingInputArchive:
        spec = BaoStockDailySpec(sessions=sessions)
        paths = tuple(sorted((root / "shards").glob("*.sqlite3")))
        if not paths:
            raise BaoStockDailyArtifactConflictError("BaoStock history manifest is unavailable")
        shards = tuple(SQLiteBaoStockDailyShard(path) for path in paths)
        context = shards[0].context(spec)
        if context is None:
            raise BaoStockDailyArtifactConflictError("BaoStock training context is unavailable")
        context_identity = shared_context_identity(spec, context, shards)
        expected = {item.code: len(context.calendar.expected_dates(item)) for item in context.universe}
        completed: set[str] = set()
        references: list[BaoStockTrainingTrainingCodeReference] = []
        shards_by_code: dict[str, SQLiteBaoStockDailyShard] = {}
        for shard in shards:
            completed.update(shard.checkpoint(spec, expected_records_by_code=expected).completed_codes)
            relative_path = shard.path.relative_to(root).as_posix()
            for identity in shard.training_code_identities(spec, frozen_identity=context_identity):
                if identity.code in shards_by_code:
                    raise BaoStockDailyArtifactConflictError("BaoStock training code is present in multiple shards")
                references.append(BaoStockTrainingTrainingCodeReference(relative_path, identity))
                shards_by_code[identity.code] = shard
        if not references:
            raise BaoStockDailyArtifactConflictError("BaoStock training-ready checkpoints are unavailable")
        manifest_path = root / "manifest.json"
        if manifest_path.is_file():
            manifest = BaoStockDailyPartitionedArchive(root).verify()
            expected_codes = frozenset(code for partition in manifest.partitions for code in partition.codes)
            if (
                not _supports_complete_nonproduction_training(manifest.audit)
                or frozenset(shards_by_code) != expected_codes
            ):
                raise BaoStockDailyArtifactConflictError("BaoStock complete manifest is not training-ready")
            input_scope: BaoStockTrainingInputScope = "complete_manifest"
            input_hash = manifest.content_hash
        else:
            if not allow_partial_history:
                raise BaoStockDailyArtifactConflictError("BaoStock history manifest is unavailable")
            input_scope = "partial_checkpoint"
            input_hash = canonical_hash(
                (
                    spec.content_hash,
                    context.calendar.content_hash,
                    canonical_hash(context.universe),
                    context.source_versions.content_hash,
                    tuple(references),
                )
            )
        snapshot = BaoStockTrainingTrainingInputSnapshot(
            input_scope,
            input_hash,
            context.calendar,
            len(context.universe),
            len(completed),
            tuple(references),
        )
        return cls(root, spec, context_identity, snapshot, shards_by_code)

    def read_training_rows(
        self,
        code: str,
        *,
        allowed_dates: frozenset[date],
    ) -> tuple[BaoStockTrainingRow, ...]:
        shard = self._shards_by_code.get(code)
        identity = self._references_by_code.get(code)
        if shard is None or identity is None:
            raise ValueError("BaoStock code is outside the frozen training input")
        return shard.read_training_rows(
            self._spec,
            code,
            allowed_dates=allowed_dates,
            frozen_identity=self._context_identity,
            expected_identity=identity,
        )


def _supports_complete_nonproduction_training(audit: BaoStockCoverageAudit) -> bool:
    if audit.status == "coverage_ready":
        return True
    return (
        audit.failure_reasons == ("null_rows_present",)
        and audit.null_rows > 0
        and not audit.failed_codes
        and audit.duplicate_rows == 0
        and audit.out_of_window_rows == 0
        and audit.future_rows == 0
        and all(item.eligible_for_training_population for item in audit.code_coverages)
    )


def _common_daily_context(
    spec: BaoStockDailySpec,
    shards: tuple[SQLiteBaoStockDailyShard, ...],
) -> BaoStockShardContext:
    first = shards[0].context(spec)
    if first is None:
        raise BaoStockDailyArtifactConflictError("BaoStock partition context is unavailable")
    for shard in shards[1:]:
        if not shard.context_matches(spec, first):
            raise BaoStockDailyArtifactConflictError("BaoStock shard daily contexts do not match")
    return first


def _stream_partition_evidence(
    root: Path,
    spec: BaoStockDailySpec,
    context: BaoStockShardContext,
    shards: tuple[SQLiteBaoStockDailyShard, ...],
    accumulator: tuple[list[BaoStockPartitionRef], dict[str, str]],
) -> Iterator[BaoStockCodeCoverageEvidence]:
    references, batch_hashes = accumulator
    for shard in shards:
        index = shard.daily_audit_index(spec, context)
        if not index.batch_hashes:
            continue
        references.append(partition_ref_from_index(root, shard, index))
        for code, batch_hash in index.batch_hashes:
            previous = batch_hashes.get(code)
            if previous is not None and previous != batch_hash:
                raise BaoStockDailyArtifactConflictError("BaoStock duplicate shard code identity conflict")
            if previous is not None:
                raise BaoStockDailyArtifactConflictError("BaoStock code is present in multiple partitions")
            batch_hashes[code] = batch_hash
        yield from index.coverage_evidence


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
        context = _common_daily_context(spec, shards)
        self._root.mkdir(parents=True, exist_ok=True)
        refs: list[BaoStockPartitionRef] = []
        batch_hashes: dict[str, str] = {}
        evidence = _stream_partition_evidence(self._root, spec, context, shards, (refs, batch_hashes))
        audit = build_baostock_coverage_audit(spec, context.calendar, context.universe, evidence)
        ordered_refs = tuple(sorted(refs, key=lambda item: item.relative_path))
        if not ordered_refs:
            raise BaoStockDailyArtifactConflictError("BaoStock partition manifest has no completed daily batches")
        manifest_codes = frozenset(code for item in ordered_refs for code in item.codes)
        if manifest_codes != frozenset(item.code for item in context.universe):
            raise BaoStockDailyArtifactConflictError("BaoStock partitions do not cover the frozen universe")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".baostock-catalog.", suffix=".sqlite3", dir=self._root)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            write_catalog_from_hashes(temporary, ordered_refs, context.universe, batch_hashes)
            checkpoint_database(temporary)
            catalog_hash = file_sha256(temporary)
            logical_hash = canonical_hash(
                tuple((item.relative_path, item.logical_records_hash) for item in ordered_refs)
            )
            manifest = BaoStockDailyManifest(
                spec_hash=spec.content_hash,
                calendar_hash=context.calendar.content_hash,
                universe_hash=canonical_hash(context.universe),
                logical_records_hash=logical_hash,
                source_versions_hash=context.source_versions.content_hash,
                source_versions=context.source_versions,
                catalog_sha256=catalog_hash,
                partitions=ordered_refs,
                audit=audit,
            )
            if self._manifest.exists() or self._catalog.exists():
                existing = self.verify()
                if existing.content_hash != manifest.content_hash:
                    raise BaoStockDailyArtifactConflictError("BaoStock partitioned artifact identity conflict")
                return existing
            os.link(temporary, self._catalog)
            write_immutable_json(self._manifest, _encode_manifest(manifest), manifest.content_hash)
            return self.verify()
        finally:
            temporary.unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
            temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)

    def verify(self) -> BaoStockDailyManifest:
        return self._read_manifest(verify_partition_hashes=True)

    def inspect_manifest(self) -> BaoStockDailyManifest:
        """Read bounded status metadata without re-hashing every sealed partition."""
        return self._read_manifest(verify_partition_hashes=False)

    def _read_manifest(self, *, verify_partition_hashes: bool) -> BaoStockDailyManifest:
        try:
            raw = _json_object(self._manifest.read_text(encoding="utf-8"))
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str):
                raise TypeError("BaoStock manifest hash is invalid")
            manifest = _decode_manifest(raw)
            if manifest.content_hash != stored_hash or manifest.catalog_sha256 != file_sha256(self._catalog):
                raise ValueError("BaoStock manifest or catalog hash mismatch")
            for reference in manifest.partitions:
                path = (self._root / reference.relative_path).resolve()
                if self._root.resolve() not in path.parents or not path.is_file() or has_pending_wal(path):
                    raise ValueError(f"BaoStock partition path is invalid: {reference.relative_path}")
                if verify_partition_hashes and file_sha256(path) != reference.database_sha256:
                    raise ValueError(f"BaoStock partition hash mismatch: {reference.relative_path}")
            return manifest
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock partition manifest is invalid") from exc

    def describe_frozen_daily_input(
        self,
        verified_manifest: BaoStockDailyManifest | None = None,
    ) -> FrozenDailyInputDescriptor:
        manifest = verified_manifest or self.verify()
        try:
            first = self._root / manifest.partitions[0].relative_path
            with sqlite3.connect(first) as connection:
                row = connection.execute("SELECT spec_json FROM context WHERE singleton=1").fetchone()
            if row is None:
                raise ValueError("BaoStock partition context is missing")
            stored_spec = _decode_spec(_json_object(row[0]))
            active_spec = BaoStockDailySpec(
                sessions=stored_spec.sessions,
                source_cutoff=stored_spec.source_cutoff,
            )
            if active_spec.content_hash != manifest.spec_hash:
                raise ValueError("BaoStock partition spec hash mismatch")
        except (TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockDailyArtifactConflictError("BaoStock partition input description is invalid") from exc
        return FrozenDailyInputDescriptor(
            manifest_hash=manifest.content_hash,
            source_identity=active_spec.research_identity,
            source_cutoff=active_spec.source_cutoff,
            requested_sessions=active_spec.sessions,
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
        spec = manifest_spec(self._root, manifest)
        return SQLiteBaoStockDailyShard(self._partition_path_for_code(code)).read_training_rows(
            spec,
            code,
            allowed_dates=allowed_dates,
        )

    def complete_dates(self, verified_manifest: BaoStockDailyManifest | None = None) -> tuple[date, ...]:
        manifest = verified_manifest or self.verify()
        if manifest.audit.status != "coverage_ready":
            return ()
        spec = manifest_spec(self._root, manifest)
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

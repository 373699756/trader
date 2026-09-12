"""Immutable storage for the historical confirmation no-data terminal."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from trader.application.research.historical_candidate_confirmation import (
    HistoricalConfirmationTerminalBatch,
)
from trader.domain.research.artifact_identity import canonical_artifact_hash, canonical_artifact_json
from trader.domain.research.h1_point_in_time import ResearchStrategy

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRATEGIES: tuple[ResearchStrategy, ...] = ("today", "tomorrow", "d25")


class HistoricalConfirmationArtifactConflictError(RuntimeError):
    """Raised when a confirmation terminal artifact is missing, changed, or conflicting."""


class HistoricalConfirmationArtifactArchive:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "historical_confirmation_terminal.json"

    def write(self, batch: HistoricalConfirmationTerminalBatch) -> HistoricalConfirmationArtifactIndex:
        index = _index(batch)
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != index.content_hash:
                raise HistoricalConfirmationArtifactConflictError(
                    "Historical confirmation terminal artifact identity conflict"
                )
            return existing
        payload = _encode(index)
        payload["content_hash"] = index.content_hash
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".historical-confirmation-terminal.", suffix=".tmp", dir=self._root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_artifact_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != index.content_hash:
                    raise HistoricalConfirmationArtifactConflictError(
                        "Historical confirmation terminal artifact identity conflict"
                    ) from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> HistoricalConfirmationArtifactIndex:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise TypeError("Historical confirmation terminal artifact is not an object")
            payload = cast(dict[str, object], raw)
            persisted_hash = payload.pop("content_hash")
            if not isinstance(persisted_hash, str) or canonical_artifact_hash(payload) != persisted_hash:
                raise ValueError("Historical confirmation terminal artifact hash mismatch")
            index = _decode(payload)
            if index.content_hash != persisted_hash:
                raise ValueError("Historical confirmation terminal artifact reconstructed hash mismatch")
            return index
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoricalConfirmationArtifactConflictError(
                "Historical confirmation terminal artifact schema or hash is invalid"
            ) from exc


@dataclass(frozen=True)
class HistoricalConfirmationArtifactIndex:
    completion_hash: str
    capability_hash: str
    label_batch_hash: str
    residual_terminal_hashes: tuple[tuple[ResearchStrategy, str], ...]
    daily_close_selection_hash: str
    strategy_terminal_hashes: tuple[tuple[ResearchStrategy, str], ...]
    joint_report_hash: str
    status: str = "historical_data_insufficient"
    terminal_holdout_status: str = "terminal_holdout_not_opened"
    production_authority: bool = False
    schema_version: str = "historical_terminal_index"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        hashes = (
            self.completion_hash,
            self.capability_hash,
            self.label_batch_hash,
            self.daily_close_selection_hash,
            self.joint_report_hash,
        )
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("Historical confirmation terminal index hash is invalid")
        residuals = _ordered_hashes(self.residual_terminal_hashes)
        strategies = _ordered_hashes(self.strategy_terminal_hashes)
        if (
            self.status != "historical_data_insufficient"
            or self.terminal_holdout_status != "terminal_holdout_not_opened"
        ):
            raise ValueError("Historical confirmation terminal index status is invalid")
        if self.production_authority or self.schema_version != "historical_terminal_index":
            raise ValueError("Historical confirmation terminal index cannot authorize production")
        object.__setattr__(self, "residual_terminal_hashes", residuals)
        object.__setattr__(self, "strategy_terminal_hashes", strategies)
        object.__setattr__(self, "content_hash", canonical_artifact_hash(self))


def _index(batch: HistoricalConfirmationTerminalBatch) -> HistoricalConfirmationArtifactIndex:
    return HistoricalConfirmationArtifactIndex(
        completion_hash=batch.parent_completion_hash,
        capability_hash=batch.parent_capability_hash,
        label_batch_hash=batch.parent_label_hash,
        residual_terminal_hashes=batch.parent_residual_ledger_hashes,
        daily_close_selection_hash=batch.parent_daily_close_selection_hash,
        strategy_terminal_hashes=tuple((item.strategy, item.content_hash) for item in batch.strategies),
        joint_report_hash=batch.joint_report_hash,
    )


def _encode(index: HistoricalConfirmationArtifactIndex) -> dict[str, object]:
    return {
        "completion_hash": index.completion_hash,
        "capability_hash": index.capability_hash,
        "label_batch_hash": index.label_batch_hash,
        "residual_terminal_hashes": [list(item) for item in index.residual_terminal_hashes],
        "daily_close_selection_hash": index.daily_close_selection_hash,
        "strategy_terminal_hashes": [list(item) for item in index.strategy_terminal_hashes],
        "joint_report_hash": index.joint_report_hash,
        "status": index.status,
        "terminal_holdout_status": index.terminal_holdout_status,
        "production_authority": index.production_authority,
        "schema_version": index.schema_version,
    }


def _decode(raw: dict[str, object]) -> HistoricalConfirmationArtifactIndex:
    expected = {
        "completion_hash",
        "capability_hash",
        "label_batch_hash",
        "residual_terminal_hashes",
        "daily_close_selection_hash",
        "strategy_terminal_hashes",
        "joint_report_hash",
        "status",
        "terminal_holdout_status",
        "production_authority",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("Historical confirmation terminal artifact fields are invalid")
    return HistoricalConfirmationArtifactIndex(
        completion_hash=_string(raw["completion_hash"]),
        capability_hash=_string(raw["capability_hash"]),
        label_batch_hash=_string(raw["label_batch_hash"]),
        residual_terminal_hashes=_hash_pairs(raw["residual_terminal_hashes"]),
        daily_close_selection_hash=_string(raw["daily_close_selection_hash"]),
        strategy_terminal_hashes=_hash_pairs(raw["strategy_terminal_hashes"]),
        joint_report_hash=_string(raw["joint_report_hash"]),
        status=_string(raw["status"]),
        terminal_holdout_status=_string(raw["terminal_holdout_status"]),
        production_authority=_boolean(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _ordered_hashes(values: tuple[tuple[ResearchStrategy, str], ...]) -> tuple[tuple[ResearchStrategy, str], ...]:
    ordered = tuple(sorted(values, key=lambda item: _STRATEGIES.index(item[0])))
    if tuple(item[0] for item in ordered) != _STRATEGIES or any(_SHA256.fullmatch(item[1]) is None for item in ordered):
        raise ValueError("Historical confirmation terminal strategy hashes are invalid")
    return ordered


def _hash_pairs(value: object) -> tuple[tuple[ResearchStrategy, str], ...]:
    if not isinstance(value, list):
        raise TypeError("Historical confirmation terminal hash pairs are invalid")
    pairs: list[tuple[ResearchStrategy, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise TypeError("Historical confirmation terminal hash pair is invalid")
        pairs.append((cast(ResearchStrategy, item[0]), item[1]))
    return tuple(pairs)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Historical confirmation terminal string field is invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Historical confirmation terminal boolean field is invalid")
    return value


__all__ = [
    "HistoricalConfirmationArtifactConflictError",
    "HistoricalConfirmationArtifactIndex",
    "HistoricalConfirmationArtifactArchive",
]

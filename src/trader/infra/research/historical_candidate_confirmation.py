"""Immutable storage for the Codex B no-data terminal."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from trader.application.research.historical_candidate_confirmation import (
    HistoricalCodexBInsufficientBatch,
)
from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.domain.research.h1_point_in_time import H1Strategy

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRATEGIES: tuple[H1Strategy, ...] = ("today", "tomorrow", "d25")


class CodexBTerminalArtifactConflictError(RuntimeError):
    """Raised when a B terminal artifact is missing, changed, or conflicting."""


class CodexBTerminalArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "codex_b_insufficient_terminal.json"

    def write(self, batch: HistoricalCodexBInsufficientBatch) -> CodexBTerminalArtifactIndex:
        index = _index(batch)
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != index.content_hash:
                raise CodexBTerminalArtifactConflictError("Codex B terminal artifact identity conflict")
            return existing
        payload = _encode(index)
        payload["content_hash"] = index.content_hash
        descriptor, temporary_name = tempfile.mkstemp(prefix=".codex-b-terminal.", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != index.content_hash:
                    raise CodexBTerminalArtifactConflictError("Codex B terminal artifact identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> CodexBTerminalArtifactIndex:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise TypeError("Codex B terminal artifact is not an object")
            payload = cast(dict[str, object], raw)
            stored_hash = payload.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(payload) != stored_hash:
                raise ValueError("Codex B terminal artifact hash mismatch")
            index = _decode(payload)
            if index.content_hash != stored_hash:
                raise ValueError("Codex B terminal artifact reconstructed hash mismatch")
            return index
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CodexBTerminalArtifactConflictError("Codex B terminal artifact schema or hash is invalid") from exc


@dataclass(frozen=True)
class CodexBTerminalArtifactIndex:
    completion_hash: str
    capability_hash: str
    label_batch_hash: str
    residual_terminal_hashes: tuple[tuple[H1Strategy, str], ...]
    c3_terminal_hash: str
    strategy_terminal_hashes: tuple[tuple[H1Strategy, str], ...]
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
            self.c3_terminal_hash,
            self.joint_report_hash,
        )
        if any(_SHA256.fullmatch(value) is None for value in hashes):
            raise ValueError("Codex B terminal index hash is invalid")
        residuals = _ordered_hashes(self.residual_terminal_hashes)
        strategies = _ordered_hashes(self.strategy_terminal_hashes)
        if (
            self.status != "historical_data_insufficient"
            or self.terminal_holdout_status != "terminal_holdout_not_opened"
        ):
            raise ValueError("Codex B terminal index status is invalid")
        if self.production_authority or self.schema_version != "historical_terminal_index":
            raise ValueError("Codex B terminal index cannot authorize production")
        object.__setattr__(self, "residual_terminal_hashes", residuals)
        object.__setattr__(self, "strategy_terminal_hashes", strategies)
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _index(batch: HistoricalCodexBInsufficientBatch) -> CodexBTerminalArtifactIndex:
    return CodexBTerminalArtifactIndex(
        completion_hash=batch.parent_completion_hash,
        capability_hash=batch.parent_capability_hash,
        label_batch_hash=batch.parent_label_hash,
        residual_terminal_hashes=batch.parent_residual_ledger_hashes,
        c3_terminal_hash=batch.parent_c3_hash,
        strategy_terminal_hashes=tuple((item.strategy, item.content_hash) for item in batch.strategies),
        joint_report_hash=batch.joint_report_hash,
    )


def _encode(index: CodexBTerminalArtifactIndex) -> dict[str, object]:
    return {
        "completion_hash": index.completion_hash,
        "capability_hash": index.capability_hash,
        "label_batch_hash": index.label_batch_hash,
        "residual_terminal_hashes": [list(item) for item in index.residual_terminal_hashes],
        "c3_terminal_hash": index.c3_terminal_hash,
        "strategy_terminal_hashes": [list(item) for item in index.strategy_terminal_hashes],
        "joint_report_hash": index.joint_report_hash,
        "status": index.status,
        "terminal_holdout_status": index.terminal_holdout_status,
        "production_authority": index.production_authority,
        "schema_version": index.schema_version,
    }


def _decode(raw: dict[str, object]) -> CodexBTerminalArtifactIndex:
    expected = {
        "completion_hash",
        "capability_hash",
        "label_batch_hash",
        "residual_terminal_hashes",
        "c3_terminal_hash",
        "strategy_terminal_hashes",
        "joint_report_hash",
        "status",
        "terminal_holdout_status",
        "production_authority",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("Codex B terminal artifact fields are invalid")
    return CodexBTerminalArtifactIndex(
        completion_hash=_string(raw["completion_hash"]),
        capability_hash=_string(raw["capability_hash"]),
        label_batch_hash=_string(raw["label_batch_hash"]),
        residual_terminal_hashes=_hash_pairs(raw["residual_terminal_hashes"]),
        c3_terminal_hash=_string(raw["c3_terminal_hash"]),
        strategy_terminal_hashes=_hash_pairs(raw["strategy_terminal_hashes"]),
        joint_report_hash=_string(raw["joint_report_hash"]),
        status=_string(raw["status"]),
        terminal_holdout_status=_string(raw["terminal_holdout_status"]),
        production_authority=_boolean(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _ordered_hashes(values: tuple[tuple[H1Strategy, str], ...]) -> tuple[tuple[H1Strategy, str], ...]:
    ordered = tuple(sorted(values, key=lambda item: _STRATEGIES.index(item[0])))
    if tuple(item[0] for item in ordered) != _STRATEGIES or any(_SHA256.fullmatch(item[1]) is None for item in ordered):
        raise ValueError("Codex B terminal strategy hashes are invalid")
    return ordered


def _hash_pairs(value: object) -> tuple[tuple[H1Strategy, str], ...]:
    if not isinstance(value, list):
        raise TypeError("Codex B terminal hash pairs are invalid")
    pairs: list[tuple[H1Strategy, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise TypeError("Codex B terminal hash pair is invalid")
        pairs.append((cast(H1Strategy, item[0]), item[1]))
    return tuple(pairs)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Codex B terminal string field is invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Codex B terminal boolean field is invalid")
    return value


__all__ = ["CodexBTerminalArtifactConflictError", "CodexBTerminalArtifactIndex", "CodexBTerminalArtifactStore"]

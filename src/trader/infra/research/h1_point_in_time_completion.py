"""Immutable artifact index for the fail-closed CodexA H1 terminal chain."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from trader.application.research.h1_point_in_time_completion import CodexAResearchCompletion
from trader.domain.research.h1_point_in_time import H1Strategy, canonical_hash

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CodexACompletionArtifactConflictError(RuntimeError):
    """Raised when a terminal index conflicts with or no longer matches its sealed hash."""


@dataclass(frozen=True)
class CodexACompletionArtifactIndex:
    completion_hash: str
    capability_hash: str
    label_batch_hash: str
    residual_terminal_hashes: tuple[tuple[H1Strategy, str], ...]
    c3_terminal_hash: str
    status: Literal["historical_data_insufficient"] = "historical_data_insufficient"
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    automatic_model_update: bool = False
    schema_version: str = "h1_terminal_index"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value in (
            self.completion_hash,
            self.capability_hash,
            self.label_batch_hash,
            self.c3_terminal_hash,
        ):
            _hash(value)
        residuals = tuple(
            sorted(self.residual_terminal_hashes, key=lambda item: ("today", "tomorrow", "d25").index(item[0]))
        )
        if tuple(item[0] for item in residuals) != ("today", "tomorrow", "d25"):
            raise ValueError("CodexA terminal index requires every strategy")
        if any(_SHA256.fullmatch(item[1]) is None for item in residuals):
            raise ValueError("CodexA residual terminal hash is invalid")
        if self.status != "historical_data_insufficient":
            raise ValueError("CodexA terminal index status is invalid")
        if self.terminal_holdout_opened or self.production_authority or self.automatic_model_update:
            raise ValueError("CodexA terminal index cannot open holdout or authorize runtime changes")
        if self.schema_version != "h1_terminal_index":
            raise ValueError("CodexA terminal index schema is invalid")
        object.__setattr__(self, "residual_terminal_hashes", residuals)
        object.__setattr__(self, "content_hash", canonical_hash(self))


class CodexACompletionArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "codex_a_h1_terminal.json"

    def write(self, completion: CodexAResearchCompletion) -> CodexACompletionArtifactIndex:
        index = _index(completion)
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != index.content_hash:
                raise CodexACompletionArtifactConflictError("CodexA terminal artifact identity conflict")
            return existing
        payload = _encode(index)
        payload["content_hash"] = index.content_hash
        descriptor, temporary_name = tempfile.mkstemp(prefix=".codex-a-h1.", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != index.content_hash:
                    raise CodexACompletionArtifactConflictError("CodexA terminal artifact identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> CodexACompletionArtifactIndex:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise TypeError("CodexA terminal artifact is not an object")
            payload = cast(dict[str, object], raw)
            stored_hash = payload.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(payload) != stored_hash:
                raise ValueError("CodexA terminal artifact hash mismatch")
            index = _decode(payload)
            if index.content_hash != stored_hash:
                raise ValueError("CodexA terminal artifact reconstructed hash mismatch")
            return index
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CodexACompletionArtifactConflictError("CodexA terminal artifact schema or hash is invalid") from exc


def _index(completion: CodexAResearchCompletion) -> CodexACompletionArtifactIndex:
    return CodexACompletionArtifactIndex(
        completion_hash=completion.content_hash,
        capability_hash=completion.capability_hash,
        label_batch_hash=completion.labels.content_hash,
        residual_terminal_hashes=tuple((item.strategy, item.content_hash) for item in completion.residual_ledgers),
        c3_terminal_hash=completion.c3.content_hash,
    )


def _encode(index: CodexACompletionArtifactIndex) -> dict[str, object]:
    return {
        "completion_hash": index.completion_hash,
        "capability_hash": index.capability_hash,
        "label_batch_hash": index.label_batch_hash,
        "residual_terminal_hashes": [list(item) for item in index.residual_terminal_hashes],
        "c3_terminal_hash": index.c3_terminal_hash,
        "status": index.status,
        "terminal_holdout_opened": index.terminal_holdout_opened,
        "production_authority": index.production_authority,
        "automatic_model_update": index.automatic_model_update,
        "schema_version": index.schema_version,
    }


def _decode(raw: dict[str, object]) -> CodexACompletionArtifactIndex:
    expected = {
        "completion_hash",
        "capability_hash",
        "label_batch_hash",
        "residual_terminal_hashes",
        "c3_terminal_hash",
        "status",
        "terminal_holdout_opened",
        "production_authority",
        "automatic_model_update",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("CodexA terminal artifact fields are invalid")
    residuals = raw["residual_terminal_hashes"]
    if not isinstance(residuals, list):
        raise TypeError("CodexA residual terminal references are invalid")
    values: list[tuple[H1Strategy, str]] = []
    for item in residuals:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(value, str) for value in item):
            raise TypeError("CodexA residual terminal reference is invalid")
        values.append((cast(H1Strategy, item[0]), cast(str, item[1])))
    return CodexACompletionArtifactIndex(
        completion_hash=_string(raw["completion_hash"]),
        capability_hash=_string(raw["capability_hash"]),
        label_batch_hash=_string(raw["label_batch_hash"]),
        residual_terminal_hashes=tuple(values),
        c3_terminal_hash=_string(raw["c3_terminal_hash"]),
        status=cast(Literal["historical_data_insufficient"], _string(raw["status"])),
        terminal_holdout_opened=_bool(raw["terminal_holdout_opened"]),
        production_authority=_bool(raw["production_authority"]),
        automatic_model_update=_bool(raw["automatic_model_update"]),
        schema_version=_string(raw["schema_version"]),
    )


def _hash(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("CodexA terminal hash is invalid")


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("CodexA terminal string field is invalid")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("CodexA terminal boolean field is invalid")
    return value


__all__ = [
    "CodexACompletionArtifactConflictError",
    "CodexACompletionArtifactIndex",
    "CodexACompletionArtifactStore",
]

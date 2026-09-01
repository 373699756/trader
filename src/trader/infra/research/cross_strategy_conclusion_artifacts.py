"""Immutable JSON storage for the cross-strategy terminal conclusion."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from trader.application.research.cross_strategy_conclusion import CrossStrategyConclusion
from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.infra.research.terminal_holdout_artifacts import (
    TerminalHoldoutArtifactConflictError,
    decode_terminal_holdout_report,
    encode_terminal_holdout_report,
)


class CrossStrategyConclusionArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, conclusion: CrossStrategyConclusion) -> CrossStrategyConclusion:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / "report.json"
        if path.exists():
            existing = self.verify()
            if existing.content_hash != conclusion.content_hash:
                raise TerminalHoldoutArtifactConflictError("cross-strategy conclusion identity conflict")
            return existing
        payload = _encode_conclusion(conclusion)
        payload["content_hash"] = conclusion.content_hash
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != conclusion.content_hash:
                    raise TerminalHoldoutArtifactConflictError("cross-strategy conclusion identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> CrossStrategyConclusion:
        path = self._root / "report.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("cross-strategy conclusion is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("cross-strategy conclusion hash mismatch")
            conclusion = _decode_conclusion(raw)
            if conclusion.content_hash != stored_hash:
                raise ValueError("cross-strategy conclusion reconstructed hash mismatch")
            return conclusion
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TerminalHoldoutArtifactConflictError("cross-strategy conclusion schema or hash is invalid") from exc


def _encode_conclusion(conclusion: CrossStrategyConclusion) -> dict[str, object]:
    return {
        "today": encode_terminal_holdout_report(conclusion.today),
        "tomorrow": encode_terminal_holdout_report(conclusion.tomorrow),
        "d25": encode_terminal_holdout_report(conclusion.d25),
        "status": conclusion.status,
        "report_hashes": [[strategy, report_hash] for strategy, report_hash in conclusion.report_hashes],
        "production_authority": conclusion.production_authority,
        "schema_version": conclusion.schema_version,
    }


def _decode_conclusion(raw: dict[str, object]) -> CrossStrategyConclusion:
    expected = {"today", "tomorrow", "d25", "status", "report_hashes", "production_authority", "schema_version"}
    if set(raw) != expected:
        raise ValueError("cross-strategy conclusion fields are invalid")
    today_raw, tomorrow_raw, d25_raw = (raw[name] for name in ("today", "tomorrow", "d25"))
    if not isinstance(today_raw, dict) or not isinstance(tomorrow_raw, dict) or not isinstance(d25_raw, dict):
        raise TypeError("cross-strategy reports are invalid")
    hashes = raw["report_hashes"]
    if not isinstance(hashes, list) or not all(isinstance(item, list) and len(item) == 2 for item in hashes):
        raise TypeError("cross-strategy report hashes are invalid")
    report_hashes = tuple((str(item[0]), str(item[1])) for item in hashes)
    status = raw["status"]
    if not isinstance(status, str):
        raise TypeError("cross-strategy status is invalid")
    authority = raw["production_authority"]
    if not isinstance(authority, bool):
        raise TypeError("cross-strategy production authority is invalid")
    schema = raw["schema_version"]
    if not isinstance(schema, str):
        raise TypeError("cross-strategy schema version is invalid")
    return CrossStrategyConclusion(
        today=decode_terminal_holdout_report(today_raw),
        tomorrow=decode_terminal_holdout_report(tomorrow_raw),
        d25=decode_terminal_holdout_report(d25_raw),
        status=status,  # type: ignore[arg-type]
        report_hashes=report_hashes,
        production_authority=authority,
        schema_version=schema,
    )


__all__ = ["CrossStrategyConclusionArtifactStore", "TerminalHoldoutArtifactConflictError"]

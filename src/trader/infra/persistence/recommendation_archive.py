"""Versioned file archive for recommendation snapshots outside the active 20-day store."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypeAlias

from trader.infra.persistence.writer_utils import _atomic_create_immutable

JsonRow: TypeAlias = Mapping[str, object]
ARCHIVE_SCHEMA = "recommendations-v1"


class RecommendationArchive:
    def __init__(self, runtime_dir: Path) -> None:
        self._runtime_dir = runtime_dir
        self.root = runtime_dir / "archive" / ARCHIVE_SCHEMA

    def store(
        self,
        *,
        snapshot_row: JsonRow,
        snapshot_payload: bytes,
        overlay_row: JsonRow | None,
        outcome_rows: Sequence[JsonRow],
    ) -> Path:
        strategy = str(snapshot_row["strategy"])
        trade_date = str(snapshot_row["recommend_date"])
        snapshot_id = str(snapshot_row["snapshot_id"])
        relative = Path("archive") / ARCHIVE_SCHEMA / trade_date / strategy / snapshot_id
        bundle = self._runtime_dir / relative
        snapshot_bytes = bytes(snapshot_payload)
        overlay_bytes = _json_bytes(dict(overlay_row)) if overlay_row is not None else b""
        outcomes_bytes = _json_bytes([dict(row) for row in outcome_rows])
        files = {
            "snapshot.json": snapshot_bytes,
            "outcomes.json": outcomes_bytes,
        }
        if overlay_bytes:
            files["overlay.json"] = overlay_bytes
        manifest = {
            "schema": ARCHIVE_SCHEMA,
            "snapshot_id": snapshot_id,
            "strategy": strategy,
            "trade_date": trade_date,
            "files": {name: _sha256(payload) for name, payload in files.items()},
            "source_manifest": dict(snapshot_row),
        }
        for name, payload in files.items():
            _atomic_create_immutable(bundle / name, payload, expected_sha256=_sha256(payload))
        manifest_bytes = _json_bytes(manifest)
        _atomic_create_immutable(
            bundle / "manifest.json",
            manifest_bytes,
            expected_sha256=_sha256(manifest_bytes),
        )
        verify_bundle(bundle)
        return relative

    def record_outcome(self, archive_relative_path: str, row: JsonRow) -> None:
        bundle = self._runtime_dir / archive_relative_path
        verify_bundle(bundle)
        stock_code = str(row["stock_code"])
        if len(stock_code) != 6 or not stock_code.isdigit():
            raise ValueError("invalid stock code in recommendation archive outcome")
        horizon = int(str(row["horizon"]))
        payload = _json_bytes(dict(row))
        target = bundle / "outcome-amendments" / f"{stock_code}-{horizon}.json"
        _atomic_create_immutable(target, payload, expected_sha256=_sha256(payload))
        digest = _sha256(payload).encode("ascii")
        _atomic_create_immutable(
            target.with_suffix(".sha256"),
            digest,
            expected_sha256=_sha256(digest),
        )

    def completed_horizons(self, archive_relative_path: str, stock_code: str) -> frozenset[int]:
        bundle = self._runtime_dir / archive_relative_path
        verify_bundle(bundle)
        completed: set[int] = set()
        raw = json.loads((bundle / "outcomes.json").read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for row in raw:
                if (
                    isinstance(row, dict)
                    and str(row.get("stock_code")) == stock_code
                    and row.get("status") == "complete"
                ):
                    horizon = row.get("horizon")
                    if isinstance(horizon, int) and not isinstance(horizon, bool):
                        completed.add(horizon)
        amendment_dir = bundle / "outcome-amendments"
        for path in amendment_dir.glob(f"{stock_code}-*.json") if amendment_dir.is_dir() else ():
            amendment = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(amendment, dict) and amendment.get("status") == "complete":
                horizon = amendment.get("horizon")
                if isinstance(horizon, int) and not isinstance(horizon, bool):
                    completed.add(horizon)
        return frozenset(completed)


def list_bundles(runtime_dir: Path) -> tuple[dict[str, str], ...]:
    root = runtime_dir / "archive" / ARCHIVE_SCHEMA
    if not root.is_dir():
        return ()
    result: list[dict[str, str]] = []
    for manifest_path in sorted(root.glob("*/*/*/manifest.json")):
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        result.append(
            {
                "trade_date": str(raw.get("trade_date") or ""),
                "strategy": str(raw.get("strategy") or ""),
                "snapshot_id": str(raw.get("snapshot_id") or ""),
                "relative_path": manifest_path.parent.relative_to(runtime_dir).as_posix(),
            }
        )
    return tuple(result)


def verify_bundle(bundle: Path) -> dict[str, object]:
    manifest_path = bundle / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != ARCHIVE_SCHEMA:
        raise ValueError("invalid recommendation archive manifest")
    files = raw.get("files")
    if not isinstance(files, dict):
        raise ValueError("recommendation archive manifest has no file hashes")
    for name, expected in files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name not in {"snapshot.json", "outcomes.json", "overlay.json"}
            or not isinstance(expected, str)
        ):
            raise ValueError("invalid recommendation archive file entry")
        target = bundle / name
        if not target.is_file() or _sha256(target.read_bytes()) != expected:
            raise ValueError(f"recommendation archive hash mismatch: {name}")
    amendment_dir = bundle / "outcome-amendments"
    if amendment_dir.is_dir():
        for target in amendment_dir.glob("*.json"):
            digest_path = target.with_suffix(".sha256")
            expected = digest_path.read_text(encoding="ascii")
            if len(expected) != 64 or _sha256(target.read_bytes()) != expected:
                raise ValueError(f"recommendation archive hash mismatch: {target.name}")
    return raw


def export_bundle(bundle: Path, output_dir: Path) -> Path:
    verify_bundle(bundle)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / bundle.name
    if target.exists():
        raise FileExistsError(f"archive export target already exists: {target}")
    shutil.copytree(bundle, target)
    verify_bundle(target)
    return target


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "ARCHIVE_SCHEMA",
    "RecommendationArchive",
    "export_bundle",
    "list_bundles",
    "verify_bundle",
]

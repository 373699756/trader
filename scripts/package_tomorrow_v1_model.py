#!/usr/bin/env python3
"""Fit and seal the manual Tomorrow V1 daily proxy into an explicit output path."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.infra.research.history_archive import SQLiteHistoricalArchive
from trader.infra.research.tomorrow_manual_v1_model import (
    TomorrowManualV1ModelArtifact,
    fit_manual_v1_model,
    sealed_production_artifact_payload,
)
from trader.infra.settings import load_runtime_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "v2" / "runtime.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runtime = load_runtime_settings(args.runtime_config.resolve())
    archive = SQLiteHistoricalArchive(runtime.runtime_dir)
    manifest = archive.manifest(SCORE_H0_V1_SPEC)
    artifact = fit_manual_v1_model(
        archive.iter_tomorrow_historical_p2_rows(SCORE_H0_V1_SPEC),
        SCORE_H0_V1_SPEC,
        manifest,
    )
    serialized = (
        json.dumps(
            sealed_production_artifact_payload(artifact),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_text(encoding="utf-8") != serialized:
            raise RuntimeError("refusing to overwrite a different Tomorrow V1 model artifact")
        _print_summary(artifact)
        return 0
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.stem}-", suffix=".json", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError:
            if output.read_text(encoding="utf-8") != serialized:
                raise RuntimeError("Tomorrow V1 model artifact identity conflict") from None
    finally:
        temporary.unlink(missing_ok=True)
    _print_summary(artifact)
    return 0


def _print_summary(artifact: TomorrowManualV1ModelArtifact) -> None:
    print(
        json.dumps(
            {
                "content_hash": artifact.content_hash,
                "feature_contract": artifact.feature_contract,
                "model_id": artifact.model_id,
                "profile_id": artifact.profile_id,
                "source_manifest_hash": artifact.source_manifest_hash,
                "training_rows": artifact.training_rows,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

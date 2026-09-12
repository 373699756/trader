from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from trader.domain.research.historical_label import H1CoverageMetadata, preregister_historical_labels
from trader.infra.research.historical_label_artifacts import (
    HistoricalLabelArtifactArchive,
    HistoricalLabelArtifactConflictError,
)


def _batch():
    dates = tuple(date(2022, 1, 1) + timedelta(days=index) for index in range(1_000))
    metadata = tuple(
        H1CoverageMetadata(strategy, "coverage_ready", dates, "a" * 64, "b" * 64, date(2026, 8, 31))
        for strategy in ("today", "tomorrow", "d25")
    )
    return preregister_historical_labels(metadata)


def test_historical_label_artifact_archive_round_trips_idempotently(tmp_path) -> None:
    batch = _batch()
    archive = HistoricalLabelArtifactArchive(tmp_path)

    assert archive.write(batch).content_hash == batch.content_hash
    assert archive.write(batch).content_hash == batch.content_hash
    assert archive.verify().content_hash == batch.content_hash


def test_historical_label_artifact_archive_rejects_conflicts_and_tampering(tmp_path) -> None:
    batch = _batch()
    archive = HistoricalLabelArtifactArchive(tmp_path)
    archive.write(batch)
    dates = tuple(date(2022, 1, 1) + timedelta(days=index) for index in range(1_000))

    conflicting = preregister_historical_labels(
        tuple(
            H1CoverageMetadata(strategy, "coverage_ready", dates, "c" * 64, "b" * 64, date(2026, 8, 31))
            for strategy in ("today", "tomorrow", "d25")
        )
    )
    with pytest.raises(HistoricalLabelArtifactConflictError, match="identity conflict"):
        archive.write(conflicting)

    payload = json.loads((tmp_path / "historical_label_preregistration.json").read_text(encoding="utf-8"))
    payload["unexpected"] = True
    (tmp_path / "historical_label_preregistration.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HistoricalLabelArtifactConflictError):
        archive.verify()

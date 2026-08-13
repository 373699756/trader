from __future__ import annotations

import json

import pytest

from tests.unit.application.research.test_historical_ports import TRADE_DATE
from tests.unit.application.research.test_score_r2_extraction import _Evaluator, _Port
from trader.application.research.extraction import ScoreR2HistoricalExtractor
from trader.infra.research.historical_partitions import (
    HistoricalPartitionConflictError,
    PolarsHistoricalPartitionStore,
)


def test_polars_partition_and_top_manifest_are_immutable_and_verifiable(tmp_path) -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()
    store = PolarsHistoricalPartitionStore(tmp_path)

    first = store.write_extraction(extraction)
    second = store.write_extraction(extraction)

    assert first == second
    assert store.verify_extraction()["extraction_hash"] == extraction.content_hash
    assert store.verify_day(TRADE_DATE).day_hash == extraction.days[0].content_hash
    manifest = json.loads((tmp_path / "2026-08-10" / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["content_hash"]) == 64
    assert {item["path"] for item in manifest["files"]} >= {
        "candidates.parquet",
        "board_coverage.parquet",
        "daily_bars.parquet",
        "evaluated_candidates.parquet",
        "hard_filter_aggregates.parquet",
        "minute_bars.parquet",
        "adjustment_windows.parquet",
        "settlements.parquet",
        "proofs.parquet",
    }

    conflicting = ScoreR2HistoricalExtractor(_Port(), _Evaluator(61.0)).extract()
    with pytest.raises(HistoricalPartitionConflictError):
        store.write_extraction(conflicting)


def test_partition_verification_rejects_tampered_file_and_top_manifest(tmp_path) -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()
    store = PolarsHistoricalPartitionStore(tmp_path)
    store.write_extraction(extraction)

    parquet = tmp_path / "2026-08-10" / "candidates.parquet"
    parquet.write_bytes(parquet.read_bytes() + b"tampered")
    with pytest.raises(HistoricalPartitionConflictError, match="file verification"):
        store.verify_day(TRADE_DATE)

    manifest_path = tmp_path / "extraction-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["status"] = "extracted"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HistoricalPartitionConflictError, match="hash mismatch"):
        store.verify_extraction()

import json
from datetime import date, timedelta

import pytest
from trader.infra.research.baostock_v3_dataset import (
    BaoStockV3DatasetArtifactConflictError,
    BaoStockV3DatasetArtifactStore,
)

from trader.domain.research.baostock_daily import (
    BaoStockBoardCoverage,
    BaoStockCoverageAudit,
    BaoStockDailyManifest,
    BaoStockSourceVersions,
    BaoStockV3LabelContract,
    BaoStockV3DatasetManifest,
    build_baostock_v3_split,
    build_baostock_v3_dataset_manifest,
)
from trader.domain.research.historical_effective_facts import (
    baostock_effective_facts_probe,
    build_historical_effective_facts_audit,
)


def _daily_manifest() -> BaoStockDailyManifest:
    digest = "a" * 64
    audit = BaoStockCoverageAudit(
        spec_hash=digest,
        calendar_hash=digest,
        universe_hash=digest,
        calendar_sessions=3,
        calendar_first_date=date(2026, 8, 29),
        calendar_last_date=date(2026, 8, 31),
        universe_count=0,
        expected_cells=0,
        obtained_cells=0,
        all_cell_coverage=0.0,
        board_coverages=tuple(BaoStockBoardCoverage(board, 0, 0, 0.0) for board in ("main", "chinext", "star")),
        code_coverages=(),
        full_window_stock_count=0,
        full_window_stocks_at_95_percent=0,
        full_window_stock_success_ratio=0.0,
        failed_codes=(),
        duplicate_rows=0,
        null_rows=0,
        out_of_window_rows=0,
        future_rows=0,
        latest_reserved_dates=(),
        status="historical_data_insufficient",
        failure_reasons=("authoritative_calendar_below_2000",),
    )
    versions = BaoStockSourceVersions("fixture", "3.14.0", (("pandas", "2.3.0"),))
    return BaoStockDailyManifest(digest, digest, digest, digest, versions.content_hash, versions, digest, audit)


def test_dataset_terminal_binds_daily_and_effective_fact_failures_without_split(tmp_path) -> None:
    facts = build_historical_effective_facts_audit((baostock_effective_facts_probe(),))
    dataset = build_baostock_v3_dataset_manifest(_daily_manifest(), facts, ())
    store = BaoStockV3DatasetArtifactStore(tmp_path)

    assert dataset.status == "historical_data_insufficient"
    assert dataset.split is None
    assert dataset.label_contract == BaoStockV3LabelContract()
    assert "historical_industry_effective_at_unavailable" in dataset.failure_reasons
    assert store.write(dataset) == dataset
    assert store.write(dataset) == dataset

    path = tmp_path / "tomorrow-v3-baostock-dataset.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "dataset_ready"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaoStockV3DatasetArtifactConflictError, match="schema or hash"):
        store.verify()


def test_ready_dataset_round_trip_binds_split_label_and_rejects_conflicting_identity(tmp_path) -> None:
    digest = "b" * 64
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1250))
    split = build_baostock_v3_split(dates, parent_manifest_hash=digest)
    manifest = BaoStockV3DatasetManifest(digest, "c" * 64, split.label_contract, "dataset_ready", split, ())
    store = BaoStockV3DatasetArtifactStore(tmp_path)

    assert store.write(manifest) == manifest
    assert store.verify() == manifest
    assert manifest.point_in_time_parity is False
    assert manifest.split is not None
    assert manifest.split.training_anchor == "15:00_daily_close"

    with pytest.raises(ValueError, match="formula"):
        BaoStockV3LabelContract(formula="different")

    conflict = BaoStockV3DatasetManifest(digest, "d" * 64, split.label_contract, "dataset_ready", split, ())
    with pytest.raises(BaoStockV3DatasetArtifactConflictError, match="identity conflict"):
        store.write(conflict)

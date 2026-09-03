from __future__ import annotations

from pathlib import Path

import pytest

from trader.application.research.baostock_history_runtime import (
    BaoStockRuntimeProgress,
    BaoStockRuntimeRequest,
    BaoStockRuntimeStatus,
)


def test_request_requires_external_absolute_runtime_and_caps_sessions(tmp_path: Path) -> None:
    request = BaoStockRuntimeRequest(runtime_dir=tmp_path, sessions=2000)
    request.validate(Path("/home/cp/Public/trader"))
    assert request.workers == 1
    with pytest.raises(ValueError, match="absolute"):
        BaoStockRuntimeRequest(runtime_dir=Path("relative"), sessions=1).validate(Path("/home/cp/Public/trader"))
    BaoStockRuntimeRequest(runtime_dir=Path("/home/cp/Public/trader/trader/data/history"), sessions=1).validate(
        Path("/home/cp/Public/trader")
    )
    with pytest.raises(ValueError, match="1..2000"):
        BaoStockRuntimeRequest(runtime_dir=tmp_path, sessions=2001).validate(Path("/home/cp/Public/trader"))
    with pytest.raises(ValueError, match="1..2000"):
        BaoStockRuntimeRequest(runtime_dir=tmp_path, sessions=True).validate(Path("/home/cp/Public/trader"))
    with pytest.raises(ValueError, match="1 or 2"):
        BaoStockRuntimeRequest(runtime_dir=tmp_path, workers=True).validate(Path("/home/cp/Public/trader"))
    with pytest.raises(ValueError, match="0..2"):
        BaoStockRuntimeRequest(runtime_dir=tmp_path, retries=True).validate(Path("/home/cp/Public/trader"))


def test_status_cannot_authorize_production() -> None:
    with pytest.raises(ValueError, match="production"):
        BaoStockRuntimeStatus(state="completed", production_authority=True)


def test_status_preserves_bounded_resource_and_resume_evidence() -> None:
    status = BaoStockRuntimeStatus(
        state="completed_with_failures",
        sessions=7,
        shard_count=2,
        universe_count=3,
        completed_codes=2,
        failed_codes=1,
        peak_rss_mb=128.5,
        historical_effective_facts_hash="a" * 64,
        v3_dataset_hash="b" * 64,
        failure_reasons=("supplier_query_failed",),
    )

    assert status.sessions == 7
    assert status.peak_rss_mb == 128.5
    assert status.historical_effective_facts_status == "historical_data_insufficient"
    assert status.v3_dataset_status == "historical_data_insufficient"
    assert status.completed_codes + status.failed_codes == status.universe_count


def test_progress_exposes_code_and_logical_record_totals_without_parallel_json_state() -> None:
    progress = BaoStockRuntimeProgress(
        phase="downloading",
        sessions=2000,
        universe_count=5211,
        completed_codes=13,
        failed_codes=2,
        expected_records=9_250_000,
        downloaded_records=23_117,
        active_workers=1,
        last_failure_reason="supplier_query_failed_blacklisted",
    )

    assert progress.checkpointed_codes == 15
    assert progress.remaining_codes == 5198
    assert progress.expected_records == 9_250_000
    assert progress.downloaded_records == 23_117

    assert BaoStockRuntimeProgress(phase="checkpoint_loading", sessions=1).phase == "checkpoint_loading"


def test_progress_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="code counts"):
        BaoStockRuntimeProgress(
            phase="downloading",
            universe_count=2,
            completed_codes=2,
            failed_codes=1,
        )
    with pytest.raises(ValueError, match="record counts"):
        BaoStockRuntimeProgress(
            phase="downloading",
            expected_records=1,
            downloaded_records=2,
        )

import json

import pandas as pd

from stock_analyzer.data_health import (
    build_and_save_data_health_report,
    build_data_health_report,
)
from stock_analyzer.pit_snapshot import PointInTimeSnapshotStore
from stock_analyzer.strategy_validation import StrategyValidationStore


def test_data_health_report_is_blocked_instead_of_ready_when_forward_data_is_empty(tmp_path):
    validation_path = str(tmp_path / "validation.sqlite3")
    StrategyValidationStore(validation_path)

    report = build_data_health_report(
        validation_path,
        str(tmp_path / "market_data.sqlite3"),
        generated_at="2026-07-14T16:00:00",
    )

    assert not report["ok"]
    assert report["status"] == "blocked"
    assert report["gates"]["P0"]["status"] == "collecting"
    assert report["gates"]["P1"]["status"] == "collecting"
    assert report["gates"]["P2"]["status"] == "blocked"
    blocker_codes = {item["code"] for item in report["blockers"]}
    assert "current_version_signal_days_insufficient" in blocker_codes
    assert "raw_market_snapshot_missing" in blocker_codes
    assert "real_forward_days_insufficient" in blocker_codes


def test_data_health_report_uses_archived_market_coverage_and_writes_daily_copy(tmp_path):
    validation_path = str(tmp_path / "validation.sqlite3")
    StrategyValidationStore(validation_path)
    quotes = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "样本",
                "price": 10.0,
                "pct_chg": 1.0,
                "turnover": 200_000_000,
                "volume_ratio": 1.2,
                "turnover_rate": 2.0,
                "amplitude": 3.0,
            }
        ]
    )
    PointInTimeSnapshotStore(validation_path).save(
        "snapshot-1",
        quotes,
        captured_at="2026-07-14T14:30:10",
        data_source_timestamp="2026-07-14T14:30:05",
    )
    report_path = tmp_path / "health.json"
    archive_dir = tmp_path / "archive"

    report = build_and_save_data_health_report(
        validation_path,
        str(tmp_path / "market_data.sqlite3"),
        output_path=str(report_path),
        archive_dir=str(archive_dir),
    )

    saved = json.loads(report_path.read_text(encoding="utf-8"))
    archived = json.loads((archive_dir / report["generated_at"][:10]).with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["point_in_time"]["market_snapshot"]["snapshot_count"] == 1
    assert saved["factor_coverage"]["gates"]["base_quote"]["passed"]
    assert saved["factor_coverage"]["gates"]["hard_filter_execution"]["passed"]
    assert archived["generated_at"] == saved["generated_at"]

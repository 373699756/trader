import sqlite3

import pandas as pd
import pytest

from stock_analyzer.pit_snapshot import PointInTimeSnapshotStore
from stock_analyzer.strategy_validation import StrategyValidationStore


def _quotes(price=10.0):
    frame = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "样本一",
                "price": price,
                "pct_chg": 1.2,
                "turnover": 300_000_000,
                "volume_ratio": 1.5,
                "turnover_rate": 2.5,
                "amplitude": 3.0,
                "industry": "银行",
            },
            {
                "code": "300001",
                "name": "ST样本二",
                "price": 20.0,
                "pct_chg": None,
                "turnover": 200_000_000,
                "volume_ratio": 1.1,
                "turnover_rate": 3.0,
                "amplitude": 4.0,
            },
        ]
    )
    frame.attrs["quote_timestamp"] = "2026-07-14T14:30:05"
    return frame


def test_pit_snapshot_persists_compressed_quotes_and_daily_universe(tmp_path):
    db_path = str(tmp_path / "validation.sqlite3")
    StrategyValidationStore(db_path)
    store = PointInTimeSnapshotStore(db_path)

    saved = store.save(
        "snapshot-1",
        _quotes(),
        captured_at="2026-07-14T14:30:10",
        data_source_timestamp="2026-07-14T14:30:05",
        source="test",
    )
    loaded = store.get("snapshot-1", include_quotes=True)

    assert saved["quote_count"] == 2
    assert saved["universe_count"] == 2
    assert saved["field_coverage"]["price"]["coverage_pct"] == 100.0
    assert saved["field_coverage"]["pct_chg"]["coverage_pct"] == 50.0
    assert loaded["quotes"][0]["code"] == "600001"
    assert loaded["quote_checksum"] == saved["quote_checksum"]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT code, is_st FROM security_universe_history ORDER BY code"
        ).fetchall()
    assert rows == [("300001", 1), ("600001", 0)]


def test_pit_snapshot_is_idempotent_and_rejects_changed_content(tmp_path):
    db_path = str(tmp_path / "validation.sqlite3")
    store = PointInTimeSnapshotStore(db_path)
    first = store.save(
        "snapshot-1",
        _quotes(),
        captured_at="2026-07-14T14:30:10",
        data_source_timestamp="2026-07-14T14:30:05",
    )
    second = store.save(
        "snapshot-1",
        _quotes(),
        captured_at="2026-07-14T14:30:10",
        data_source_timestamp="2026-07-14T14:30:05",
    )

    assert first["status"] == "saved"
    assert second["status"] == "already_saved"
    with pytest.raises(ValueError, match="different quote content"):
        store.save(
            "snapshot-1",
            _quotes(price=11.0),
            captured_at="2026-07-14T14:30:10",
            data_source_timestamp="2026-07-14T14:30:05",
        )


def test_pit_snapshot_summary_tracks_independent_trade_days(tmp_path):
    store = PointInTimeSnapshotStore(str(tmp_path / "validation.sqlite3"))
    for index, day in enumerate(("14", "15"), start=1):
        store.save(
            "snapshot-{}".format(index),
            _quotes(),
            captured_at="2026-07-{}T14:30:10".format(day),
            data_source_timestamp="2026-07-{}T14:30:05".format(day),
        )

    summary = store.summary()

    assert summary["snapshot_count"] == 2
    assert summary["trade_day_count"] == 2
    assert summary["universe_stock_count"] == 2
    assert summary["date_start"] == "2026-07-14"
    assert summary["date_end"] == "2026-07-15"

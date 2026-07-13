import os

import pandas as pd

from stock_analyzer.daily_data import (
    DailyMarketDataStore,
    list_market_data_codes,
    load_execution_history_frames,
    load_history_frames,
)
from stock_analyzer.factor_snapshot import FactorSnapshotStore, build_factor_snapshots


def _history(code):
    return pd.DataFrame(
        [
            {
                "trade_date": "20240101",
                "code": code,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
                "turnover": 1000,
                "pct_chg": 0,
            },
            {
                "trade_date": "20240102",
                "code": code,
                "open": 10,
                "high": 12,
                "low": 10,
                "close": 11,
                "volume": 120,
                "turnover": 1320,
                "pct_chg": 10,
            },
        ]
    )


def _long_history(code, rows=30):
    data = []
    for index in range(rows):
        close = 10 + index * 0.2
        data.append(
            {
                "trade_date": "202401{:02d}".format(index + 1),
                "code": code,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000 + index * 10,
                "turnover": (1000 + index * 10) * close,
                "pct_chg": 0 if index == 0 else 2,
            }
        )
    return pd.DataFrame(data)


def test_market_data_store_can_use_business_object_shards(tmp_path):
    db_dir = tmp_path / "market_data"
    store = DailyMarketDataStore(str(db_dir))

    store.upsert_stock_meta(
        [
            {"code": "600000", "name": "A", "is_active": True},
            {"code": "300750", "name": "B", "is_active": True},
        ]
    )
    assert store.upsert_bars("600000", _history("600000"), _history("600000")) == 2
    assert store.upsert_bars("300750", _history("300750"), _history("300750")) == 2
    store.record_status("600000", "A", "ok", last_trade_date="20240102", row_count=2)

    assert os.path.exists(db_dir / "market_data_meta.sqlite3")
    assert os.path.exists(db_dir / "market_data_bars_main_60000.sqlite3")
    assert os.path.exists(db_dir / "market_data_bars_chinext_30075.sqlite3")
    assert store.latest_trade_date("600000") == "20240102"

    summary = store.summary()
    assert summary["sharded"] is True
    assert summary["shard_count"] == 2
    assert summary["bar_count"] == 4
    assert summary["stock_count"] == 2
    assert summary["status"] == {"ok": 1}

    frames = load_history_frames(str(db_dir), ["600000", "300750"], days=2)
    assert sorted(frames) == ["300750", "600000"]
    assert frames["600000"]["price"].tolist() == [10, 11]


def test_market_data_code_listing_ignores_old_shard_names(tmp_path):
    db_dir = tmp_path / "market_data"
    store = DailyMarketDataStore(str(db_dir))
    assert store.upsert_bars("600000", _history("600000"), _history("600000")) == 2

    old_shard = db_dir / "market_data_bars_main_600.sqlite3"
    old_store = DailyMarketDataStore(str(old_shard))
    assert old_store.upsert_bars("600999", _history("600999"), _history("600999")) == 2

    assert list_market_data_codes(str(db_dir)) == ["600000"]
    assert store.summary()["stock_count"] == 1


def test_execution_history_keeps_raw_prices_separate_from_qfq_factors(tmp_path):
    db_dir = tmp_path / "market_data"
    store = DailyMarketDataStore(str(db_dir))
    raw = _history("600000")
    qfq = raw.copy()
    for column in ("open", "high", "low", "close"):
        qfq[column] = qfq[column] * 2
    assert store.upsert_bars("600000", raw, qfq) == 2

    factor_frame = load_history_frames(str(db_dir), ["600000"], days=2)["600000"]
    execution_frame = load_execution_history_frames(str(db_dir), ["600000"], days=2)["600000"]

    assert factor_frame["price"].tolist() == [20, 22]
    assert execution_frame["price"].tolist() == [10, 11]
    assert factor_frame.attrs["price_adjustment_mode"] == "qfq"
    assert execution_frame.attrs["price_adjustment_mode"] == "raw"


def test_factor_snapshots_build_from_market_data(tmp_path):
    market_dir = tmp_path / "market_data"
    snapshot_db = tmp_path / "factor_snapshots.sqlite3"
    store = DailyMarketDataStore(str(market_dir))
    assert store.upsert_bars("600000", _long_history("600000"), _long_history("600000")) == 30
    assert store.upsert_bars("300750", _long_history("300750"), _long_history("300750")) == 30

    result = build_factor_snapshots(
        str(market_dir),
        str(snapshot_db),
        days=30,
        batch_size=1,
    )

    assert result["ok"] is True
    assert result["requested"] == 2
    assert result["saved"] == 2
    assert result["summary"]["row_count"] == 2
    assert result["summary"]["stock_count"] == 2

    latest = FactorSnapshotStore(str(snapshot_db)).latest(limit=5)
    assert len(latest) == 2
    assert latest[0]["trade_date"] == "20240130"
    assert "ret_20d" in latest[0]["factors"]
    lookup = FactorSnapshotStore(str(snapshot_db)).lookup(
        [
            {"signal_date": "2024-01-30", "code": "600000"},
            {"trade_date": "20240130", "code": "300750"},
            {"signal_date": "2024-01-29", "code": "600000"},
        ]
    )
    assert sorted(lookup) == [("20240130", "300750"), ("20240130", "600000")]
    assert "ma20_gap" in lookup[("20240130", "600000")]

    second = build_factor_snapshots(str(market_dir), str(snapshot_db), days=30)
    assert second["saved"] == 2
    assert second["summary"]["row_count"] == 2

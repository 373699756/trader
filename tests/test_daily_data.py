import os

import pandas as pd

from stock_analyzer.daily_data import DailyMarketDataStore, list_market_data_codes, load_history_frames


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

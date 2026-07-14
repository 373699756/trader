import sqlite3

import pandas as pd

from stock_analyzer.pit_snapshot import PointInTimeSnapshotStore
from stock_analyzer.strategy_validation import StrategyValidationStore


def test_missing_signal_provenance_is_unknown_and_replay_version_is_proxy(tmp_path):
    db_path = str(tmp_path / "validation.sqlite3")
    store = StrategyValidationStore(db_path)
    first = store.save_signals(
        "tomorrow_picks",
        "tomorrow_picks_v12_post_1430_t1_exit",
        "2026-07-14T14:30:00",
        [{"rank": 1, "code": "600001", "name": "manual", "price": 10, "score": 80}],
    )
    replay = store.save_signals(
        "tomorrow_picks",
        "tomorrow_picks_replay_v2",
        "2026-07-13T15:00:00",
        [{"rank": 1, "code": "600002", "name": "replay", "price": 10, "score": 80}],
    )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT strategy_version, sample_type FROM strategy_signal_batches ORDER BY signal_date"
        ).fetchall()

    assert first["sample_type"] == "unknown"
    assert replay["sample_type"] == "daily_proxy_replay"
    assert rows == [
        ("tomorrow_picks_replay_v2", "daily_proxy_replay"),
        ("tomorrow_picks_v12_post_1430_t1_exit", "unknown"),
    ]


def test_raw_snapshot_requires_explicit_type_and_valid_intraday_timestamp(tmp_path):
    quotes = pd.DataFrame([{"code": "600001", "name": "sample", "price": 10.0}])
    store = PointInTimeSnapshotStore(str(tmp_path / "validation.sqlite3"))
    default = store.save(
        "default",
        quotes,
        captured_at="2026-07-14T14:30:10",
        data_source_timestamp="2026-07-14T14:30:05",
    )
    outside_window = store.save(
        "outside",
        quotes,
        captured_at="2026-07-14T15:01:00",
        data_source_timestamp="2026-07-14T15:00:59",
        sample_type="real_forward",
    )
    live = store.save(
        "live",
        quotes,
        captured_at="2026-07-14T14:31:00",
        data_source_timestamp="2026-07-14T14:30:59",
        sample_type="real_forward",
    )

    assert default["sample_type"] == "unknown"
    assert outside_window["sample_type"] == "real_forward"
    assert live["sample_type"] == "real_forward"

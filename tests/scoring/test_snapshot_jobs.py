import io
import json
import sys
import threading
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from helpers import app_patch_context
from stock_analyzer import config, daily_job
from stock_analyzer.app_response_support import saved_tomorrow_fallback_payload
from stock_analyzer.scoring import prepare_candidates
from stock_analyzer.snapshot import SNAPSHOT_STRATEGIES, _score_snapshot_strategy, run_snapshot
from stock_analyzer.strategy_validation import StrategyValidationStore
from stock_analyzer.validation_backup import backup_validation_db, list_validation_backups, restore_validation_db
from stock_analyzer.validation_runtime_support import run_validation_auto_update_once


def _snapshot_quotes(count=12):
    return pd.DataFrame(
        [
            {
                "code": f"600{index:03d}",
                "name": f"样本{index}",
                "price": 10 + index * 0.1,
                "pct_chg": 2.0 + (index % 4) * 0.2,
                "speed": 0.4,
                "volume_ratio": 1.5,
                "turnover_rate": 4,
                "turnover": 200000000 + index * 1000000,
                "industry": "半导体" if index % 2 else "电力",
                "sixty_day_pct": 12,
                "ytd_pct": 16,
                "amplitude": 4,
            }
            for index in range(count)
        ]
    )


def test_daily_job_factor_ic_writes_file(tmp_path):
    samples = [
        {"raw": {"fundamental_quality_score": 90}, "primary_return_net": 3.0},
        {"raw": {"fundamental_quality_score": 50}, "primary_return_net": 1.0},
        {"raw": {"fundamental_quality_score": 10}, "primary_return_net": -2.0},
    ]
    factor_path = tmp_path / "factor_ic.json"
    argv = ["daily_job", "--factor-ic", "--strategy", "tomorrow_picks"]

    with patch.object(sys, "argv", argv), patch.object(config, "FACTOR_IC_PATH", str(factor_path)), patch.object(
        config,
        "VALIDATION_DB_PATH",
        str(tmp_path / "validation.sqlite3"),
    ), patch(
        "stock_analyzer.daily_job.StrategyValidationStore.live_weight_samples",
        return_value=samples,
    ):
        with redirect_stdout(io.StringIO()):
            result = daily_job.main()
    payload = json.loads(factor_path.read_text(encoding="utf-8"))

    assert result == 0
    assert payload["ic"]["fundamental_quality_score"]["ic"] > 0


def test_daily_job_strategy_all_and_explicit_strategy_sets():
    validation, executable = daily_job._task_strategy_sets("all", SNAPSHOT_STRATEGIES)
    assert validation == list(config.AUTO_SNAPSHOT_STRATEGIES)
    assert executable == list(config.ACTIVE_STRATEGIES)

    validation, executable = daily_job._task_strategy_sets("short_term", SNAPSHOT_STRATEGIES)
    assert validation == ["short_term"]
    assert executable == []


def test_validation_auto_update_summarizes_pending_reasons():
    status = {"running": False}

    def set_status(**values):
        status.update(values)

    result = run_validation_auto_update_once(
        auto_update_lock=threading.Lock(),
        auto_update_status=status,
        set_auto_update_status=set_status,
        run_validation_outcome_update_once_fn=lambda: {
            "ok": True,
            "updates": [
                {
                    "strategy": "tomorrow_picks",
                    "result": {
                        "requested": 2,
                        "updated": 1,
                        "pending": 1,
                        "skipped": 1,
                        "execution_skipped": 0,
                        "skipped_reasons": {"not_mature_no_future_trade": 1},
                    },
                },
                {
                    "strategy": "swing_picks",
                    "result": {
                        "requested": 1,
                        "updated": 0,
                        "pending": 0,
                        "skipped": 1,
                        "execution_skipped": 1,
                        "skipped_reasons": {"unbuyable_limit_up": 1},
                    },
                },
            ],
        },
    )

    assert result["ok"]
    assert result["summary"]["requested"] == 3
    assert result["summary"]["updated"] == 1
    assert result["summary"]["pending"] == 1
    assert result["summary"]["skipped_reasons"]["not_mature_no_future_trade"] == 1
    assert result["summary"]["skipped_reasons"]["unbuyable_limit_up"] == 1


def test_daily_job_after_close_runs_market_data_snapshot_update_and_backup(tmp_path):
    calls = []

    class FakeStore:
        def __init__(self, path):
            self.path = path

        def update_outcomes(self, provider, strategy_name=""):
            calls.append(("update", strategy_name))
            return {"updated": 0}

        def live_weight_samples(self, strategy_name, days=120):
            return []

    def fake_download_market_data(**kwargs):
        calls.append(("download", kwargs.get("limit")))
        return {"ok": True, "downloaded": 1, "failed": 0}

    def fake_run_snapshots(provider, store, strategies, market="all"):
        calls.append(("snapshot", tuple(strategies)))
        return [{"ok": True}]

    argv = ["daily_job", "--after-close", "--strategy", "tomorrow_picks", "--market-data-limit", "3"]
    with patch.object(sys, "argv", argv), patch.object(
        config,
        "VALIDATION_DB_PATH",
        str(tmp_path / "validation.sqlite3"),
    ), patch(
        "stock_analyzer.market_data.download_market_data",
        side_effect=fake_download_market_data,
    ), patch(
        "stock_analyzer.strategy_validation.StrategyValidationStore",
        FakeStore,
    ), patch(
        "stock_analyzer.providers.MarketDataProvider",
        return_value=object(),
    ), patch(
        "stock_analyzer.snapshot.run_snapshots",
        side_effect=fake_run_snapshots,
    ), patch(
        "stock_analyzer.factor_snapshot.build_factor_snapshots",
        return_value={"ok": True, "count": 0},
    ), patch(
        "stock_analyzer.factor_ic.compute_factor_ic",
        return_value={"sample_count": 0, "ic": {}},
    ), patch(
        "stock_analyzer.factor_ic.save_factor_ic",
        return_value=None,
    ), patch(
        "stock_analyzer.validation_backup.backup_validation_db",
        return_value={"ok": True},
    ):
        with redirect_stdout(io.StringIO()) as stdout:
            result = daily_job.main()
    payload = json.loads(stdout.getvalue())

    assert result == 0
    assert calls[0] == ("download", 3)
    assert ("snapshot", ("tomorrow_picks",)) not in calls
    assert ("update", "tomorrow_picks") in calls
    assert payload["market_data"]["downloaded"] == 1


def test_daily_job_after_close_fails_when_market_data_empty(tmp_path):
    argv = ["daily_job", "--after-close", "--strategy", "tomorrow_picks"]
    with patch.object(sys, "argv", argv), patch.object(
        config,
        "VALIDATION_DB_PATH",
        str(tmp_path / "validation.sqlite3"),
    ), patch(
        "stock_analyzer.market_data.download_market_data",
        return_value={
            "ok": True,
            "requested": 3,
            "downloaded": 0,
            "skipped": 0,
            "failed": 3,
            "summary": {"bar_count": 0},
        },
    ), patch(
        "stock_analyzer.snapshot.run_snapshots",
        side_effect=AssertionError("should not save snapshots without usable market data"),
    ):
        with redirect_stdout(io.StringIO()) as stdout:
            result = daily_job.main()
    payload = json.loads(stdout.getvalue())

    assert result == 1
    assert not payload["ok"]
    assert payload["error"] == "market_data_unavailable"


def test_validation_backup_roundtrip_restores_database(tmp_path):
    db_path = str(tmp_path / "validation.sqlite3")
    backup_path = str(tmp_path / "strategy_validation.backup.sqlite3")
    store = StrategyValidationStore(db_path)
    store.save_signals(
        "tomorrow_picks",
        "tomorrow_picks_v1",
        "2026-07-08T15:00:00",
        [{"rank": 1, "code": "600001", "name": "备份样本", "price": 10, "score": 80}],
    )
    backup = backup_validation_db(db_path, backup_path, label="test")
    store.save_signals(
        "tomorrow_picks",
        "tomorrow_picks_v1",
        "2026-07-09T15:00:00",
        [{"rank": 1, "code": "600002", "name": "待还原样本", "price": 11, "score": 81}],
    )

    restored = restore_validation_db(backup["path"], db_path, backup_path)
    restored_store = StrategyValidationStore(db_path)
    dates = restored_store.list_signal_dates("tomorrow_picks")
    backups = list_validation_backups(backup_path)

    assert backup["ok"]
    assert restored["ok"]
    assert [row["signal_date"] for row in dates] == ["2026-07-08"]
    assert [item["name"] for item in backups] == ["strategy_validation.backup.sqlite3"]


def test_daily_job_can_list_validation_backups_without_provider(tmp_path):
    argv = ["daily_job", "--list-validation-backups"]
    backup_path = str(tmp_path / "strategy_validation.backup.sqlite3")

    with patch.object(sys, "argv", argv), patch.object(config, "VALIDATION_BACKUP_PATH", backup_path):
        with redirect_stdout(io.StringIO()) as stdout:
            result = daily_job.main()
    payload = json.loads(stdout.getvalue())

    assert result == 0
    assert payload["ok"]
    assert payload["backups"] == []


def test_run_snapshot_saves_strategy_rows_without_web_route(tmp_path):
    class FakeProvider:
        def get_realtime_quotes(self):
            return _snapshot_quotes()

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    with patch.object(config, "QUOTE_SNAPSHOT_MIN_ROWS", 1), patch.object(
        config, "TOMORROW_SIGNAL_CUTOFF_TIME", "23:59"
    ), patch(
        "stock_analyzer.snapshot._signal_at_or_after_freeze_cutoff", return_value=False
    ):
        result = run_snapshot(FakeProvider(), store, "tomorrow_picks", market="all")
    dates = store.list_signal_dates("tomorrow_picks")

    assert result["ok"]
    assert result["saved"]["saved"] > 0
    assert dates[0]["strategy_name"] == "tomorrow_picks"


def test_auto_snapshot_includes_intraday_observation_without_making_it_executable():
    assert "short_term" in config.AUTO_SNAPSHOT_STRATEGIES
    assert "short_term" not in config.ACTIVE_STRATEGIES


def test_snapshot_tomorrow_keeps_wide_validation_candidates():
    quotes = pd.DataFrame(
        [
            {
                "code": f"600{index:03d}",
                "name": f"宽样本{index}",
                "price": 10 + index * 0.1,
                "pct_chg": 2.0 + (index % 4) * 0.2,
                "speed": 0.2,
                "volume_ratio": 1.5,
                "turnover_rate": 4,
                "turnover": 500000000 + index * 10000000,
                "industry": f"宽样本行业{index}",
                "sixty_day_pct": 12,
                "ytd_pct": 18,
                "amplitude": 4,
            }
            for index in range(12)
        ]
    )
    candidates = prepare_candidates(quotes)

    with patch.object(config, "TOMORROW_SNAPSHOT_TOP_N", 12), patch.object(
        config,
        "TOMORROW_RECOMMENDATION_DISPLAY_LIMIT",
        4,
    ), patch("stock_analyzer.strategies.tomorrow.tomorrow_policy._tomorrow_display_gate", return_value=(12, 0.0, "测试展示全部候选")):
        rows, meta, version = _score_snapshot_strategy(
            None,
            candidates,
            quotes,
            "tomorrow_picks",
            "all",
            {"level": "risk_on", "label": "偏进攻", "score": 75},
        )

    assert version == config.TOMORROW_STRATEGY_VERSION
    assert len(rows) > config.TOMORROW_RECOMMENDATION_DISPLAY_LIMIT
    assert meta["top_n"] == 12
    assert meta["display_cap"] == 0
    assert meta["display_limit"] == 12
    assert len(meta["_candidate_pool_rows"]) >= len(rows)
    assert all("frozen_rule_rank" in row for row in meta["_candidate_pool_rows"])


def test_snapshot_short_and_swing_capture_full_scored_pools():
    quotes = _snapshot_quotes(12)
    candidates = prepare_candidates(quotes)
    market_regime = {"level": "risk_on", "label": "偏进攻", "score": 75}

    for strategy in ("short_term", "swing_picks"):
        rows, meta, _version = _score_snapshot_strategy(
            None,
            candidates,
            quotes,
            strategy,
            "all",
            market_regime,
        )
        assert len(meta["_candidate_pool_rows"]) >= len(rows)
        assert all("frozen_rule_rank" in row for row in meta["_candidate_pool_rows"])


def test_saved_tomorrow_fallback_uses_display_cap_not_wide_snapshot_count():
    class FakeStore:
        def live_weight_samples(self, strategy_name, days=60):
            return []

    saved_rows = [
        {"rank": index + 1, "code": f"600{index:03d}", "name": f"保存样本{index}", "score": 90 - index}
        for index in range(12)
    ]

    with patch.object(config, "TOMORROW_RECOMMENDATION_DISPLAY_LIMIT", 4):
        payload = saved_tomorrow_fallback_payload(
            saved_rows=saved_rows,
            top_n=12,
            market="all",
            detailed=True,
            validation_store=FakeStore(),
            cached_metrics_fn=lambda strategy, days: {"sample_count": 0},
            load_risk_blacklist_fn=lambda: {},
            analysis_window_fn=lambda: "15:00",
            provider_health_fn=lambda: {},
            research_disclaimer_fn=lambda: "",
        )

    assert len(payload["data"]) == 4
    assert payload["meta"]["candidate_count"] == 12
    assert payload["meta"]["display_count"] == 4
    assert payload["meta"]["display_limit"] == 4


def test_run_snapshot_rejects_local_quote_snapshot_when_disabled(tmp_path):
    class FakeProvider:
        def get_realtime_quotes(self):
            return _snapshot_quotes(60)

        def health(self):
            return {
                "quotes_source": "本地快照",
                "last_quote_refresh": datetime.now().isoformat(timespec="seconds"),
            }

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    with patch.object(config, "VALIDATION_ALLOW_LOCAL_QUOTE_SNAPSHOT", False):
        result = run_snapshot(FakeProvider(), store, "tomorrow_picks", market="all")
    dates = store.list_signal_dates("tomorrow_picks")

    assert not result["ok"]
    assert result["saved"]["saved"] == 0
    assert dates == []
    assert "本地快照" in result["error"]


def test_run_snapshot_allows_fresh_local_quote_snapshot_when_enabled(tmp_path):
    class FakeProvider:
        def get_realtime_quotes(self):
            return _snapshot_quotes(60)

        def health(self):
            return {
                "quotes_source": "本地快照",
                "last_quote_refresh": datetime.now().isoformat(timespec="seconds"),
            }

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    with patch.object(config, "VALIDATION_ALLOW_LOCAL_QUOTE_SNAPSHOT", True), patch.object(
        config,
        "TOMORROW_SIGNAL_CUTOFF_TIME",
        "23:59",
    ), patch(
        "stock_analyzer.snapshot._signal_at_or_after_freeze_cutoff", return_value=False,
    ):
        result = run_snapshot(FakeProvider(), store, "tomorrow_picks", market="all")
    dates = store.list_signal_dates("tomorrow_picks")

    assert result["ok"]
    assert result["saved"]["saved"] > 0
    assert dates[0]["strategy_name"] == "tomorrow_picks"


def test_tomorrow_snapshot_rejects_when_freeze_completes_at_cutoff(tmp_path):
    rows = [{"rank": 1, "code": "699999", "name": "缺失锚点", "price": 10.0, "score": 80}]
    meta = {"generated_at": "2026-07-08T15:05:00", "top_n": 1}

    class FakeProvider:
        def get_realtime_quotes(self):
            return _snapshot_quotes(60)

        def get_history(self, code, days=10):
            return pd.DataFrame([{"trade_date": "20260707", "price": 9.5}])

        def health(self):
            return {
                "quotes_source": "东方财富直连",
                "last_quote_refresh": datetime.now().isoformat(timespec="seconds"),
            }

    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    with patch("stock_analyzer.snapshot._score_snapshot_strategy", return_value=(rows, meta, "tomorrow_picks_test")), patch(
        "stock_analyzer.snapshot._signal_at_or_after_freeze_cutoff",
        side_effect=[False, True],
    ):
        result = run_snapshot(FakeProvider(), store, "tomorrow_picks", market="all")
    dates = store.list_signal_dates("tomorrow_picks")

    assert not result["ok"]
    assert dates == []
    assert "完成冻结" in result["error"]


def test_prefetch_history_endpoint_downloads_then_updates_validation(tmp_path):
    history = pd.DataFrame(
        {
            "trade_date": ["20240101", "20240102", "20240103", "20240104"],
            "open": [10, 12, 12.5, 13],
            "high": [10.5, 13, 13.2, 13.6],
            "low": [9.8, 11.8, 12.0, 12.7],
            "price": [10, 12.5, 13.0, 13.5],
        }
    )
    validation_path = tmp_path / "validation.sqlite3"
    store = StrategyValidationStore(str(validation_path))
    store.save_signals(
        "tomorrow_picks",
        "tomorrow_picks_v2",
        "2024-01-01T14:30:00",
        [{"rank": 1, "code": "600001", "name": "样本", "price": 10, "score": 90}],
    )

    with patch(
        "stock_analyzer.providers.MarketDataProvider.prefetch_history",
        return_value={"requested": 1, "unique_codes": 1, "downloaded": 1, "cached": 0, "failed": 0, "errors": []},
    ), patch(
        "stock_analyzer.providers.MarketDataProvider.get_history",
        return_value=history,
    ), patch(
        "stock_analyzer.providers.MarketDataProvider.get_execution_history",
        return_value=history,
    ), app_patch_context(
        tmp_path,
        VALIDATION_DB_PATH=str(validation_path),
        TOMORROW_HIGH_OPEN_SKIP_PCT=50.0,
    ) as app:
        response = app.test_client().post(
            "/api/strategy-validation/prefetch-history?strategy=tomorrow_picks&date=2024-01-01&update=1"
        )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"]
    assert payload["prefetch"]["downloaded"] == 1
    assert payload["outcome"]["updated"] == 1
    assert payload["codes"][0]["code"] == "600001"

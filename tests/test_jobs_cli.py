import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

pytest.importorskip("pandas")

from stock_analyzer import config
from stock_analyzer.jobs import main as jobs_main


class _FakeStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.calls = []
        self.metrics_called = False
        self._metrics = {"ok": True}

    def update_outcomes(self, *_, **__):
        self.calls.append("update_outcomes")
        return {"ok": True, "requested": 0, "updated": 0}

    def update_outcomes_once(self, *_, **__):
        return self.update_outcomes()

    @property
    def repository(self):
        return self

    def applied_migrations(self):
        return ["0001_bootstrap_schema", "0011_add_query_indexes"]

    def table_exists(self, table_name: str):
        return False

    def metrics(self, strategy_name: str = "", days: int = 20):
        self.metrics_called = True
        return {"ok": True, "strategy": strategy_name, "days": days}


def _run_jobs_cli(argv, *, config_db_path, config_backup_path, config_active=""):
    with patch.object(config, "VALIDATION_DB_PATH", config_db_path), patch.object(
        config,
        "VALIDATION_BACKUP_PATH",
        config_backup_path,
    ), patch.object(config, "AUTO_SNAPSHOT_STRATEGIES", ["tomorrow_picks"]), patch.object(
        config,
        "ACTIVE_STRATEGIES",
        ["tomorrow_picks"],
    ), patch("stock_analyzer.jobs.StrategyValidationStore", _FakeStore):
        if config_active:
            with patch.object(config, "AUTO_SNAPSHOT_STRATEGIES", [config_active]), patch.object(
                config, "ACTIVE_STRATEGIES", [config_active]
            ):
                with redirect_stdout(io.StringIO()) as stdout:
                    code = jobs_main(argv)
        else:
            with redirect_stdout(io.StringIO()) as stdout:
                code = jobs_main(argv)
    text = stdout.getvalue().strip()
    payload = json.loads(text) if text else {}
    return code, payload


def _run_jobs_cli_patched(argv, config_db_path, config_backup_path):
    return _run_jobs_cli(argv, config_db_path=config_db_path, config_backup_path=config_backup_path)


def test_snapshot_and_update_commands_record_metrics_and_dispatch(tmp_path):
    with patch("stock_analyzer.jobs.run_snapshots", return_value=[{"ok": True}]), patch(
        "stock_analyzer.jobs.MarketDataProvider",
        return_value=object(),
    ):
        code, snapshot_payload = _run_jobs_cli_patched(
            ["snapshot", "--strategy", "tomorrow_picks"],
            config_db_path=str(tmp_path / "validation.sqlite3"),
            config_backup_path=str(tmp_path / "validation.backup.sqlite3"),
        )

    assert code == 0
    assert snapshot_payload["ok"] is True
    assert snapshot_payload["command"] == "snapshot"
    assert snapshot_payload["strategy_count"] == 1
    assert snapshot_payload["metrics"]["success_count"] >= 1

    code, update_payload = _run_jobs_cli_patched(
        ["update-outcomes", "--strategy", "tomorrow_picks"],
        config_db_path=str(tmp_path / "validation.sqlite3"),
        config_backup_path=str(tmp_path / "validation.backup.sqlite3"),
    )
    assert code == 0
    assert update_payload["command"] == "update-outcomes"
    assert update_payload["ok"] is True
    assert update_payload["metrics"]["success_count"] >= 1


def test_stats_command_exposes_readiness_and_backup_snapshot(tmp_path):
    with patch("stock_analyzer.jobs.build_validation_readiness_report", return_value={"ok": True, "status": "ready"}), patch(
        "stock_analyzer.jobs.list_validation_backups",
        return_value=[
            {"path": "/tmp/backup/strategy_validation.backup.sqlite3.gz", "mode": "compressed", "bytes": 99, "name": "x.gz"},
            {"path": "/tmp/backup/strategy_validation.backup.sqlite3", "mode": "single_file", "bytes": 88, "name": "latest.sqlite3"},
        ],
    ):
        code, stats_payload = _run_jobs_cli_patched(
            ["stats"],
            config_db_path=str(tmp_path / "validation.sqlite3"),
            config_backup_path=str(tmp_path / "validation.backup.sqlite3"),
        )

    assert code == 0
    assert stats_payload["ok"] is True
    assert stats_payload["readiness"]["ok"] is True
    assert stats_payload["migrations"]["count"] >= 1
    assert stats_payload["backups"]["count"] == 2
    assert stats_payload["metrics"]["success_count"] >= 1


def test_tune_command_passes_days_and_strategy_filter(tmp_path):
    with patch("stock_analyzer.jobs.run_validation_tuning_once", return_value={"ok": True, "runs": []}) as tuning_call:
        with patch("stock_analyzer.jobs.deepseek_validation_review", return_value={"ok": True}):
            code, tune_payload = _run_jobs_cli_patched(
                ["tune", "--strategy", "tomorrow_picks", "--days", "13", "--no-deepseek"],
                config_db_path=str(tmp_path / "validation.sqlite3"),
                config_backup_path=str(tmp_path / "validation.backup.sqlite3"),
            )

    assert code == 0
    assert tune_payload["ok"] is True
    assert tune_payload["strategies"] == ["tomorrow_picks"]
    assert tune_payload["tuning"]["ok"] is True
    tuning_call.assert_called_once()
    assert tuning_call.call_args.kwargs["days"] == 13
    assert tuning_call.call_args.kwargs["use_deepseek"] is False


def test_command_failure_records_error_and_failure_metric(tmp_path):
    with patch("stock_analyzer.jobs.run_snapshots", side_effect=RuntimeError("boom")):
        code, payload = _run_jobs_cli_patched(
            ["snapshot", "--strategy", "tomorrow_picks"],
            config_db_path=str(tmp_path / "validation.sqlite3"),
            config_backup_path=str(tmp_path / "validation.backup.sqlite3"),
        )

    assert code == 1
    assert payload["ok"] is False
    assert payload["command"] == "snapshot"
    assert "boom" in str(payload.get("error", ""))
    assert payload["metrics"]["failure_count"] >= 1
    assert payload["metrics"]["last_error"] == "boom"
    assert payload["metrics"]["elapsed_seconds"] >= 0


def test_backup_command_forwards_label_and_keep_and_exposes_result(tmp_path):
    backup_payload = {
        "ok": True,
        "label": "nightly",
        "path": str(tmp_path / "strategy_validation.backup.sqlite3"),
        "bytes": 12345,
        "mode": "single_file",
    }

    with patch("stock_analyzer.jobs.backup_validation_db", return_value=backup_payload) as backup_call:
        code, payload = _run_jobs_cli_patched(
            ["backup", "--label", "nightly", "--keep", "3"],
            config_db_path=str(tmp_path / "validation.sqlite3"),
            config_backup_path=str(tmp_path / "validation.backup.sqlite3"),
        )

    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "backup"
    assert payload["label"] == "nightly"
    assert payload["path"] == backup_payload["path"]
    assert payload["metrics"]["success_count"] >= 1

    backup_call.assert_called_once_with(
        str(tmp_path / "validation.sqlite3"),
        str(tmp_path / "validation.backup.sqlite3"),
        label="nightly",
        keep=3,
    )


def test_update_outcomes_command_aggregates_summary_fields_from_each_strategy(tmp_path):
    outcomes = [
        {"requested": 10, "updated": 7, "skipped": 1, "pending": 1, "unknown": 1, "execution_skipped": 0, "error_count": 0},
        {"requested": 6, "updated": 5, "skipped": 0, "pending": 1, "unknown": 0, "execution_skipped": 1, "error_count": 0},
    ]

    class _FakeOutcomeStore:
        def __init__(self, path: str) -> None:
            self.path = path
            self._remaining = list(outcomes)

        def update_outcomes(self, *_, **__):
            if not self._remaining:
                return {"ok": True, "requested": 0, "updated": 0, "skipped": 0, "pending": 0, "unknown": 0, "execution_skipped": 0, "error_count": 0}
            return self._remaining.pop(0)

    with patch.object(config, "VALIDATION_DB_PATH", str(tmp_path / "validation.sqlite3")), patch.object(
        config, "VALIDATION_BACKUP_PATH", str(tmp_path / "validation.backup.sqlite3")
    ), patch.object(config, "AUTO_SNAPSHOT_STRATEGIES", ["tomorrow_picks"]), patch.object(
        config, "ACTIVE_STRATEGIES", ["tomorrow_picks", "swing_picks"]
    ), patch("stock_analyzer.jobs.StrategyValidationStore", _FakeOutcomeStore), patch(
        "stock_analyzer.jobs.MarketDataProvider", return_value=object()
    ), redirect_stdout(io.StringIO()) as stdout:
        code = jobs_main(["update-outcomes", "--strategy", "tomorrow_picks,swing_picks"])

    assert code == 0
    payload = json.loads(stdout.getvalue().strip())
    assert payload["ok"] is True
    summary = payload["summary"]
    assert summary["requested"] == 16
    assert summary["updated"] == 12
    assert summary["skipped"] == 1
    assert summary["pending"] == 2
    assert summary["unknown"] == 1
    assert summary["execution_skipped"] == 1
    assert summary["error_count"] == 0
    assert "elapsed_seconds" in payload["metrics"]
    assert "lock_wait_seconds" in payload["metrics"]
    assert payload["metrics"]["elapsed_seconds"] >= 0
    assert payload["metrics"]["lock_wait_seconds"] >= 0
    assert payload["metrics"]["success_count"] >= 1

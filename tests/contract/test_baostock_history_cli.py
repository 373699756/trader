from __future__ import annotations

from pathlib import Path

import pytest

from trader.entrypoints.cli import build_parser


def test_download_history_is_explicit_and_bounded() -> None:
    args = build_parser().parse_args(
        ["download_history", "--runtime-dir", "/tmp/trader-baostock", "--sessions", "2000"]
    )
    assert args.command == "download_history"
    assert args.runtime_dir == Path("/tmp/trader-baostock")
    assert args.sessions == 2000


def test_download_history_defaults_to_ignored_repository_history_directory() -> None:
    args = build_parser().parse_args(["download_history"])
    assert args.runtime_dir == Path("trader/data/history")


def test_download_history_rejects_more_than_2000_during_argument_parsing(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "must-not-exist"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["download_history", "--runtime-dir", str(runtime_dir), "--sessions", "2001"])

    assert not runtime_dir.exists()


def test_download_history_contract_exposes_typed_live_progress_and_two_database_roles() -> None:
    from trader.application.research.baostock_history_runtime import BaoStockRuntimeProgress

    progress = BaoStockRuntimeProgress(
        phase="database_initializing",
        sessions=1,
        universe_count=5211,
        expected_records=5211,
    )

    assert progress.schema_version == "baostock_runtime_progress_v1"
    assert progress.universe_count == progress.expected_records

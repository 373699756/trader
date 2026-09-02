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


def test_download_history_rejects_more_than_2000_during_argument_parsing(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "must-not-exist"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["download_history", "--runtime-dir", str(runtime_dir), "--sessions", "2001"])

    assert not runtime_dir.exists()

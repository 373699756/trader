from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pytest

from trader.application.research.baostock_history_runtime import BaoStockRuntimeProgress, BaoStockRuntimeStatus
from trader.entrypoints import cli
from trader.entrypoints.cli import _BaoStockProgressWriter


def test_cli_progress_writer_prints_a_compact_human_summary() -> None:
    stream = io.StringIO()
    clock = iter((0.0, 3_723.5))
    writer = _BaoStockProgressWriter(
        Path("/var/lib/trader/history"), sessions=2000, stream=stream, monotonic=lambda: next(clock)
    )

    writer.publish(
        BaoStockRuntimeProgress(
            phase="downloading",
            current_code="600001",
            sessions=2000,
            universe_count=5211,
            completed_codes=13,
            training_ready_codes=8,
            failed_codes=2,
            expected_records=9_250_000,
            downloaded_records=23_117,
            active_workers=1,
            last_failure_reason="supplier_query_failed_blacklisted",
        )
    )

    assert stream.getvalue() == (
        "[downloading] 已下载/总数：13/5,211，完成进度：0.25%，总下载条数：23,117，未下载：5,198，"
        "训练可用/总数：8/5,211，待补训练事实：5,203，"
        "耗时：1时02分03秒，当前 600001，失败原因：supplier_query_failed_blacklisted，"
        "保存文件：*.sqlite3\n"
    )


def test_cli_progress_writer_reports_zero_percent_before_the_universe_is_known() -> None:
    stream = io.StringIO()
    writer = _BaoStockProgressWriter(
        Path("/var/lib/trader/history"), sessions=2000, stream=stream, monotonic=lambda: 0.0
    )

    writer.publish(BaoStockRuntimeProgress(phase="preflight", sessions=2000))

    assert "完成进度：0.00%" in stream.getvalue()


def test_download_cli_translates_first_signal_into_typed_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_events: list[str] = []

    class _Controller:
        exit_code = 130

        def __init__(self, *, timeout_seconds: float, on_first_signal: Callable[[object], None]) -> None:
            assert timeout_seconds == 10.0
            self._on_first_signal = on_first_signal

        def install(self) -> None:
            controller_events.append("installed")
            self._on_first_signal(object())

        def mark_completed(self) -> None:
            controller_events.append("completed")

        def restore(self) -> None:
            controller_events.append("restored")

    def _run(
        request: object,
        repository_root: object,
        *,
        cancel_requested: Callable[[], bool],
        progress: object,
    ) -> BaoStockRuntimeStatus:
        del request, repository_root, progress
        assert cancel_requested()
        return BaoStockRuntimeStatus(state="cancelled", failure_reasons=("cancelled",))

    monkeypatch.setattr(cli, "ShutdownSignalController", _Controller)
    monkeypatch.setattr("trader.infra.research.baostock_history_runtime.run_baostock_history", _run)

    assert cli._run_baostock_history(tmp_path, 2000) == 130
    assert controller_events == ["installed", "completed", "restored"]

from __future__ import annotations

import io
from pathlib import Path

from trader.application.research.baostock_history_runtime import BaoStockRuntimeProgress
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
            failed_codes=2,
            expected_records=9_250_000,
            downloaded_records=23_117,
            active_workers=1,
            last_failure_reason="supplier_query_failed_blacklisted",
        )
    )

    assert stream.getvalue() == (
        "[downloading] 已下载/总数：13/5,211，完成进度：0.25%，总下载条数：23,117，未下载：5,198，"
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

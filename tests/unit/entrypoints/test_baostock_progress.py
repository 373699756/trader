from __future__ import annotations

import io
from pathlib import Path

from trader.application.research.baostock_history_runtime import BaoStockRuntimeProgress
from trader.entrypoints.cli import _BaoStockProgressWriter


def test_cli_progress_writer_prints_a_compact_human_summary() -> None:
    stream = io.StringIO()
    writer = _BaoStockProgressWriter(
        Path("/var/lib/trader/history"), sessions=2000, stream=stream, monotonic=lambda: 12.5
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
        "[downloading] 已下载 13 只/23,117 条，未下载 5,198 只，失败 2 只，"
        "耗时 0分00秒，当前 600001，最近失败 supplier_query_failed_blacklisted，"
        "路径 /var/lib/trader/history/baostock-daily/sessions-2000\n"
    )

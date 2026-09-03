from __future__ import annotations

import io
import json
from pathlib import Path

from trader.application.research.baostock_history_runtime import BaoStockRuntimeProgress
from trader.entrypoints.cli import _BaoStockProgressWriter


def test_cli_progress_writer_flushes_stage_totals_and_database_locations() -> None:
    stream = io.StringIO()
    writer = _BaoStockProgressWriter(
        Path("/var/lib/trader/history"), sessions=2000, stream=stream, monotonic=lambda: 12.5
    )

    writer.publish(
        BaoStockRuntimeProgress(
            phase="downloading",
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

    payload = json.loads(stream.getvalue())
    assert payload == {
        "active_workers": 1,
        "checkpoint_database_pattern": "/var/lib/trader/history/baostock-daily/sessions-2000/shard-*.sqlite3",
        "completed_codes": 13,
        "downloaded_records": 23117,
        "elapsed_seconds": 0.0,
        "expected_records": 9250000,
        "failed_codes": 2,
        "final_database": "/var/lib/trader/history/baostock-daily/sessions-2000/score-baostock-daily-core-v2.sqlite3",
        "last_failure_reason": "supplier_query_failed_blacklisted",
        "phase": "downloading",
        "checkpointed_codes": 15,
        "remaining_codes": 5198,
        "schema_version": "baostock_runtime_progress_v1",
        "sessions": 2000,
        "universe_count": 5211,
    }

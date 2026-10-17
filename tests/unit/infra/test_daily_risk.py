from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneBusyError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.recommendation.infra.persistence.daily_risk import SQLiteDailyRiskIndex

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_daily_risk_write_probe_routes_to_selected_partition(tmp_path) -> None:
    selected = date(2026, 10, 19)
    index = SQLiteDailyRiskIndex(tmp_path / "risk", target_date=lambda: selected)
    database = index.prepare(selected)

    with closing(sqlite3.connect(database)) as locker:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(DataPlaneBusyError):
            index.probe_writable(timeout_seconds=0.001)
        locker.rollback()
    index.probe_writable()


def _risk_record(observed_at: datetime) -> RiskEvidenceRecord:
    return RiskEvidenceRecord(
        code="600001",
        observed_at=observed_at,
        source="fixture",
        source_time=observed_at,
        data_version="risk:1",
        evidence_id="risk-observation:structured",
        payload={"status": "known_clear"},
    )


def test_daily_risk_index_uses_date_filename_and_seeds_previous_partition(tmp_path) -> None:
    selected = date(2026, 10, 16)
    index = SQLiteDailyRiskIndex(tmp_path / "risk", target_date=lambda: selected)
    observed_at = datetime(2026, 10, 16, 9, 15, tzinfo=SHANGHAI)
    index.save_risk_evidence_recent(_risk_record(observed_at))
    index.save_source_cursor_recent(
        SourceCursorRecord(
            cursor_name="fixture:600001",
            observed_at=observed_at,
            source="fixture",
            source_time=observed_at,
            data_version="cursor:1",
            payload={"history_complete": True},
            cursor_value="1",
        )
    )

    selected = date(2026, 10, 19)
    next_index = SQLiteDailyRiskIndex(tmp_path / "risk", target_date=lambda: selected)
    database = next_index.prepare(selected)

    assert database == tmp_path / "risk" / "2026-10-19.sqlite3"
    assert next_index.load_risk_evidence_recent("600001", "risk-observation:structured") is not None
    assert next_index.load_source_cursor_recent("fixture:600001") is not None
    assert not tuple((tmp_path / "risk").glob("*.building*"))


def test_daily_risk_index_does_not_read_a_later_partition(tmp_path) -> None:
    selected = date(2026, 10, 20)
    later = SQLiteDailyRiskIndex(tmp_path / "risk", target_date=lambda: selected)
    observed_at = datetime(2026, 10, 20, 9, 15, tzinfo=SHANGHAI)
    later.save_risk_evidence_recent(_risk_record(observed_at))

    selected = date(2026, 10, 19)
    earlier = SQLiteDailyRiskIndex(tmp_path / "risk", target_date=lambda: selected)

    assert earlier.load_risk_evidence_recent_records() == ()

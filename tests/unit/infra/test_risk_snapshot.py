from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.recommendation.infra.persistence.data_plane import SQLiteDataPlane
from trader.recommendation.infra.persistence.risk_snapshot import (
    PublishRiskSnapshot,
    RiskDatabaseKind,
    RiskDatabasePath,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 10, 19, 9, 15, tzinfo=SHANGHAI)


def _plane(root, kind: RiskDatabaseKind) -> SQLiteDataPlane:
    return SQLiteDataPlane(root, database_path=RiskDatabasePath(root, kind))


def _risk_record(*, source: str, evidence_id: str) -> RiskEvidenceRecord:
    return RiskEvidenceRecord(
        code="600001",
        observed_at=OBSERVED_AT,
        source=source,
        source_time=OBSERVED_AT,
        data_version=f"{source}:1",
        evidence_id=evidence_id,
        payload={"status": "known_clear"},
    )


def test_risk_database_paths_are_fixed_by_task(tmp_path) -> None:
    assert RiskDatabasePath(tmp_path, "risk_download") == tmp_path / "risk_download.sqlite3"
    assert RiskDatabasePath(tmp_path, "risk_history_download") == tmp_path / "risk_history_download.sqlite3"
    assert RiskDatabasePath(tmp_path, "risk_snapshot") == tmp_path / "risk_snapshot.sqlite3"


def test_history_first_waits_for_current_database_before_publication(tmp_path) -> None:
    history = _plane(tmp_path, "risk_history_download")
    history.save_risk_evidence_recent(_risk_record(source="cninfo", evidence_id="cninfo-announcement:fixture"))

    assert PublishRiskSnapshot(tmp_path) is None
    assert not RiskDatabasePath(tmp_path, "risk_snapshot").exists()

    current = _plane(tmp_path, "risk_download")
    current.save_risk_evidence_recent(_risk_record(source="akshare", evidence_id="risk-observation:structured"))
    snapshot = PublishRiskSnapshot(tmp_path)
    reader = SQLiteDataPlane(tmp_path, database_path=snapshot, read_only=True)

    assert {record.source for record in reader.load_risk_evidence_recent_records()} == {"akshare", "cninfo"}


def test_later_task_publication_merges_current_and_history(tmp_path) -> None:
    current = _plane(tmp_path, "risk_download")
    current.save_risk_evidence_recent(_risk_record(source="akshare", evidence_id="risk-observation:structured"))
    first_snapshot = PublishRiskSnapshot(tmp_path)
    assert first_snapshot == RiskDatabasePath(tmp_path, "risk_snapshot")

    history = _plane(tmp_path, "risk_history_download")
    history.save_risk_evidence_recent(_risk_record(source="cninfo", evidence_id="cninfo-announcement:fixture"))
    history.save_source_cursor_recent(
        SourceCursorRecord(
            cursor_name="cninfo.announcements:600001",
            observed_at=OBSERVED_AT,
            source="cninfo",
            source_time=OBSERVED_AT,
            data_version="cninfo:cursor:1",
            payload={"history_complete": True},
            cursor_value="1",
        )
    )

    second_snapshot = PublishRiskSnapshot(tmp_path)
    reader = SQLiteDataPlane(tmp_path, database_path=second_snapshot, read_only=True)

    records = reader.load_risk_evidence_recent_records()
    assert {(record.source, record.evidence_id) for record in records} == {
        ("akshare", "risk-observation:structured"),
        ("cninfo", "cninfo-announcement:fixture"),
    }
    assert reader.load_source_cursor_recent("cninfo.announcements:600001") is not None
    with pytest.raises(DataPlaneUnavailableError, match="read-only"):
        reader.save_risk_evidence_recent(_risk_record(source="akshare", evidence_id="risk-component:news"))


def test_invalid_history_keeps_previously_published_snapshot(tmp_path) -> None:
    current = _plane(tmp_path, "risk_download")
    current.save_risk_evidence_recent(_risk_record(source="akshare", evidence_id="risk-observation:structured"))
    snapshot = PublishRiskSnapshot(tmp_path)
    assert snapshot is not None
    published = snapshot.read_bytes()

    history = _plane(tmp_path, "risk_history_download")
    history.save_risk_evidence_recent(_risk_record(source="akshare", evidence_id="risk-component:financial"))

    with pytest.raises(DataPlaneUnavailableError, match="another task"):
        PublishRiskSnapshot(tmp_path)

    assert snapshot.read_bytes() == published
    assert not tuple(tmp_path.glob("*.building*"))

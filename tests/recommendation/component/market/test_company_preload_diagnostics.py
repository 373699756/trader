"""Company preload diagnostics preserve native codes without exposing query values."""

from contextlib import closing
import json
import sqlite3
import sys

import pytest

from tests.helpers.recommendation.market_data import NOW
from tests.helpers.shared.configuration import PublishedCompanyResearchFixture
from trader.infra.settings.models import CompanyPreloadSettings


class _FailedReader:
    def __init__(self, stage, failure):
        self.stage = stage
        self.failure = failure
        self.versions = 0
        self.visits = 0

    def _fail(self):
        try:
            if self.failure == "native":
                with closing(sqlite3.connect(":memory:")) as connection:
                    connection.execute("SELECT * FROM private_company_payload")
            else:
                raise sqlite3.DatabaseError(self.failure)
        except sqlite3.Error as exc:
            raise RuntimeError("private-company-payload /private/company.sqlite3") from exc

    def publication_version(self):
        self.versions += 1
        if self.stage == "before" or (self.stage == "after" and self.versions == 2):
            self._fail()
        return "publication"

    def visit(self, codes, consume, *, cancel_requested=None):
        self.visits += 1
        assert tuple(codes) == tuple(sorted(set(codes)))
        assert len(codes) == 4402
        assert cancel_requested is not None and not cancel_requested()
        if self.stage == "visit":
            self._fail()


def _initialize(reader):
    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: NOW, open_dates=lambda: (NOW.date(),), monotonic=lambda: 0.0,
        preload_settings=CompanyPreloadSettings(timeout_seconds=30, publication_attempts=3),
    )
    codes = tuple(f"{code:06d}" for code in range(4402))
    research.initialize(tuple(reversed(codes)), cancelled=lambda: False)
    return research, codes


@pytest.mark.parametrize("stage", ("before", "visit", "after"))
@pytest.mark.parametrize("failure", ("native", "sqlite_schema_unsupported"))
def test_preload_failure_records_actual_sqlite_code_or_explicit_absence_and_safe_full_frames(stage, failure, caplog):
    reader = _FailedReader(stage, failure)
    research, codes = _initialize(reader)

    assert research.status().last_error == "company_snapshot:RuntimeError"
    records = [record for record in caplog.records if record.name.endswith("published_company_research.sqlite")]
    assert len(records) == 1
    detail = json.loads(records[0].getMessage().partition(" detail=")[2])
    assert f"stage={stage}" in records[0].getMessage()
    assert [item["error_type"] for item in detail] == ["RuntimeError", "OperationalError" if failure == "native" else "DatabaseError"]
    assert len(detail[0]["frames"]) >= 3
    assert any(":_observe_snapshot_read:" in frame for frame in detail[0]["frames"])
    assert any(":_fail:" in frame for frame in detail[1]["frames"])
    if failure == "native":
        assert detail[1]["sqlite_errorcode"] == (sqlite3.SQLITE_ERROR if sys.version_info >= (3, 11) else None)
        assert detail[1]["sqlite_errorname"] == ("SQLITE_ERROR" if sys.version_info >= (3, 11) else None)
        assert detail[1]["internal_code"] is None
    else:
        assert detail[1]["sqlite_errorcode"] is None
        assert detail[1]["sqlite_errorname"] is None
        assert detail[1]["internal_code"] == failure
    assert "private_company_payload" not in caplog.text
    assert "private-company-payload" not in caplog.text
    assert "/private/company.sqlite3" not in caplog.text
    calls = reader.versions, reader.visits
    assert calls == (2 if stage == "after" else 1, 0 if stage == "before" else 1)
    research.initialize(codes, cancelled=lambda: False)
    assert (reader.versions, reader.visits) == calls


@pytest.mark.parametrize("failure", (
    "sqlite_required_row_missing", "sqlite_integrity_failed", "private-company-payload /private/company.sqlite3",
))
def test_handwritten_database_error_only_exposes_known_internal_identifiers(failure, caplog):
    _initialize(_FailedReader("visit", failure))
    records = [record for record in caplog.records if record.name.endswith("published_company_research.sqlite")]
    assert len(records) == 1
    detail = json.loads(records[0].getMessage().partition(" detail=")[2])[-1]
    assert detail["sqlite_errorcode"] is None and detail["sqlite_errorname"] is None
    assert detail["internal_code"] == (failure if failure.startswith("sqlite_") else None)
    assert "private-company-payload" not in caplog.text
    assert "/private/company.sqlite3" not in caplog.text

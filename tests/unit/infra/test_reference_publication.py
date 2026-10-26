"""Only a validated reference from the same Shanghai date supplies a cached calendar."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.reference_fixtures import PreparedFixture
from trader.download.infra.akshare_calendar import AkshareCalendarTimeoutError
from trader.download.infra.reference_publication import ReferencePublication

_NOW = datetime(2026, 10, 26, 20, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_same_day_calendar_reuse_preserves_reference_and_avoids_supplier(tmp_path):
    publication = ReferencePublication(tmp_path)
    reference = PreparedFixture(_NOW)
    publication.publish(reference)
    before = (tmp_path / "inputs.json").read_bytes()
    events = []

    def fetch():
        pytest.fail("same-day calendar must not depend on another supplier request")

    assert publication.calendar(_NOW.date(), fetch=fetch, progress=events.append) == reference.open_dates
    assert [(event.component, event.status) for event in events] == [("calendar", "reused")]
    assert (tmp_path / "inputs.json").read_bytes() == before


@pytest.mark.parametrize("cache", ("missing", "yesterday", "future", "corrupt", "hash_conflict", "empty_calendar"))
def test_missing_stale_or_invalid_calendar_requires_supplier(tmp_path, cache):
    publication = ReferencePublication(tmp_path)
    reference = PreparedFixture(_NOW)
    if cache != "missing":
        offset = -1 if cache == "yesterday" else 1 if cache == "future" else 0
        publication.publish(PreparedFixture(_NOW + timedelta(days=offset)))
    path = tmp_path / "inputs.json"
    if cache == "corrupt":
        path.write_text("{", encoding="utf-8")
    elif cache in {"hash_conflict", "empty_calendar"}:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["identity" if cache == "hash_conflict" else "open_dates"] = "0" * 64 if cache == "hash_conflict" else []
        path.write_text(json.dumps(raw), encoding="utf-8")
    events, calls = [], []

    def fetch():
        calls.append(True)
        return reference.open_dates

    assert publication.calendar(_NOW.date(), fetch=fetch, progress=events.append) == reference.open_dates
    assert calls == [True]
    assert [event.status for event in events] == ["running", "complete"]
    assert bool(events[0].failure) == (cache in {"corrupt", "hash_conflict", "empty_calendar"})


def test_expired_calendar_does_not_hide_timeout_or_replace_previous_publication(tmp_path):
    publication = ReferencePublication(tmp_path)
    publication.publish(PreparedFixture(_NOW - timedelta(days=1)))
    before = (tmp_path / "inputs.json").read_bytes()

    def fetch():
        raise AkshareCalendarTimeoutError(30)

    with pytest.raises(AkshareCalendarTimeoutError):
        publication.calendar(_NOW.date(), fetch=fetch, progress=None)
    assert (tmp_path / "inputs.json").read_bytes() == before

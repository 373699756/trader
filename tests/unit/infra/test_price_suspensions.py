from datetime import date
import json

import pytest

from trader.download.domain.price_suspension import PriceSuspension, PriceSuspensionSnapshot
from trader.download.infra.price_suspensions import PriceSuspensionSource
from trader.infra.http_contracts import HttpResponse

DAY = date(2026, 11, 5)


def _execute(operation, timeout_seconds, *, cancel_requested):
    assert timeout_seconds == 5
    assert not cancel_requested()
    return operation()


def _row(**changes):
    return {
        "SECURITY_CODE": "002129",
        "SUSPEND_EXPIRE": "连续停牌",
        "SUSPEND_START_TIME": "2026-11-02 09:30:00",
        "SUSPEND_END_TIME": "2026-11-13 15:00:00",
        "PREDICT_RESUME_DATE": "2026-11-16 00:00:00",
        **changes,
    }


def _fetch(rows, **result_changes):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        payload = {"success": True, "result": {"pages": 1, "count": len(rows), "data": rows, **result_changes}}
        return HttpResponse(200, json.dumps(payload).encode())

    return PriceSuspensionSource(get, _execute, lambda: False).fetch(DAY), calls


def test_dated_batch_retains_continuous_interval_but_never_projects_future_days():
    snapshot, calls = _fetch([_row()])
    assert snapshot.suspensions == (PriceSuspension("002129", date(2026, 11, 2), DAY),)
    assert snapshot.dates_for("002129", (date(2026, 10, 30), DAY, date(2026, 11, 6))) == frozenset((DAY,))
    assert snapshot.dates_for("000001", (DAY,)) == frozenset()
    assert len(calls) == 1
    assert "DATETIME='2026-11-05'" in calls[0][1]["params"]["filter"]
    assert calls[0][1]["timeout"] == 3


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"SUSPEND_END_TIME": None, "PREDICT_RESUME_DATE": None}, True),
        ({"SUSPEND_END_TIME": "2026-11-05 15:00:00"}, True),
        ({"SUSPEND_END_TIME": "2026-11-05 10:30:00"}, False),
        ({"SUSPEND_EXPIRE": "盘中停牌"}, False),
        ({"SUSPEND_START_TIME": "2026-11-05 10:00:00"}, False),
        ({"SUSPEND_START_TIME": "2026-11-06 09:30:00"}, False),
        ({"PREDICT_RESUME_DATE": "2026-11-05 00:00:00"}, False),
    ],
)
def test_partial_sessions_and_resume_day_are_never_filtered(changes, expected):
    snapshot, _calls = _fetch([_row(**changes)])
    assert bool(snapshot.dates_for("002129", (DAY,))) is expected


@pytest.mark.parametrize("changes", [{"pages": 2}, {"count": 2}, {"data": None}])
def test_incomplete_batch_is_rejected_instead_of_becoming_a_partial_filter(changes):
    with pytest.raises(ValueError, match="price_suspension_list_incomplete"):
        _fetch([_row()], **changes)


def test_empty_success_is_valid_but_duplicate_or_malformed_rows_are_rejected():
    assert _fetch([])[0] == PriceSuspensionSnapshot(DAY, ())
    with pytest.raises(ValueError, match="price_suspension_snapshot_invalid"):
        _fetch([_row(), _row()])
    with pytest.raises(ValueError, match="price_suspension_date_invalid"):
        _fetch([_row(SUSPEND_START_TIME="invalid")])


def test_provider_failure_does_not_become_suspension_evidence():
    def get(*_args, **_kwargs):
        return HttpResponse(200, b'{"success":false,"result":null}')

    with pytest.raises(ValueError, match="price_suspension_response_invalid"):
        PriceSuspensionSource(get, _execute, lambda: False).fetch(DAY)

from __future__ import annotations

import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.process_lock import ProcessLock
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek import budget_file
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.budget_batch_ledger import BudgetBatchCompletion, BudgetBatchRequest
from trader.recommendation.infra.deepseek.budget_file import BudgetStateFile
from trader.recommendation.infra.deepseek.health_gate import DeepSeekHealthPolicy

NOW = datetime(2026, 10, 29, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _ledger(root, *, runtime="runtime", limit=168, enabled=True, policy=None):
    return DeepSeekBudgetLedger(
        BudgetStateFile(root / runtime / "state.json", ProcessLock(root / "data/.deepseek.lock", label="DeepSeek")),
        daily_hard_limit=limit,
        strategy_limits={"tomorrow": limit, "d25": 0, "shared_preheat": 0, "emergency": 0},
        stage_targets={"tomorrow_morning": 0},
        stage_limits={"tomorrow_morning": limit},
        health_policy=policy,
        enabled=enabled,
    )


def _ready(root, **options):
    ledger = _ledger(root, **options)
    ledger.initialize(NOW - timedelta(days=1))
    ledger.initialize(NOW)
    return ledger


def _reserve(ledger, at=NOW, **options):
    return ledger.reserve(Strategy.TOMORROW, phase="morning_main", requested_at=at, **options)


def _batch(ledger, at=NOW, profile="v2"):
    return ledger.begin_batch(BudgetBatchRequest(
        Strategy.TOMORROW, "morning_main", "tomorrow", "fixture", at, at + timedelta(minutes=1),
        ("600001",), profile=profile,
    ))


def _complete(ledger, batch, at=NOW, *, success=False):
    reservation = _reserve(ledger, at, batch_id=batch, candidate_count=1)
    assert reservation.allowed
    ledger.finish(reservation.reservation_id, status="success" if success else "failed", completed_at=at)
    review = DeepSeekReview(
        code="600001", outcome=ReviewOutcome.APPLIED if success else ReviewOutcome.REJECTED,
        dimensions={}, risk_facts=(), completed_at=at,
    )
    ledger.finish_batch(BudgetBatchCompletion(batch, "success" if success else "failed", at, {review.code: review}, 1))


def test_unknown_first_day_is_quarantined_across_restart_and_recovers_next_day(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.initialize(NOW)
    assert _reserve(ledger).reason == "budget_state_unverified"
    ledger.close()
    restarted = _ledger(tmp_path)
    restarted.initialize(NOW)
    assert not _reserve(restarted).allowed
    assert _reserve(restarted, NOW + timedelta(days=1)).allowed
    restarted.close()


def test_168_concurrent_attempts_are_shared_across_four_profiles(tmp_path):
    ledger = _ready(tmp_path)

    def request(index):
        batch = _batch(ledger, profile=("v1", "v2", "v3", "v5")[index % 4])
        reservation = _reserve(ledger, batch_id=batch)
        if reservation.allowed:
            ledger.finish(reservation.reservation_id, status="failed", completed_at=NOW)
        ledger.finish_batch(BudgetBatchCompletion(batch, "failed" if reservation.allowed else "skipped", NOW, {}, int(reservation.allowed)))
        return reservation.allowed

    with ThreadPoolExecutor(max_workers=16) as pool:
        allowed = tuple(pool.map(request, range(240)))
    assert sum(allowed) == 168
    state = json.loads((tmp_path / "runtime/state.json").read_text())
    assert len(state["requests"]) == len(state["review_batches"]) == 168
    assert {item["profile"] for item in state["requests"]} == {"v1", "v2", "v3", "v5"}
    assert ledger.summary(NOW.date().isoformat())["used"] == 168
    ledger.close()


def test_restart_charges_reserved_but_unsent_requests_and_config_changes(tmp_path):
    ledger = _ready(tmp_path, limit=4)
    batch = _batch(ledger)
    assert _reserve(ledger, batch_id=batch).allowed
    ledger.close()
    restarted = _ledger(tmp_path, limit=1)
    restarted.initialize(NOW)
    summary = restarted.summary(NOW.date().isoformat())
    assert summary["call_status"]["abandoned"] == 1
    assert summary["batch_status"]["abandoned"] == 1
    assert _reserve(restarted).reason == "daily_hard_limit"
    restarted.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "version", "negative", "naive", "too_large"])
def test_unverifiable_file_cannot_replenish_today(tmp_path, damage):
    ledger = _ready(tmp_path)
    assert _reserve(ledger).allowed
    ledger.close()
    path = tmp_path / "runtime/state.json"
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{")
    elif damage == "too_large":
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    else:
        state = json.loads(path.read_text())
        if damage == "version":
            state["schema_version"] = 999
        elif damage == "negative":
            state["requests"][0]["token_count"] = -1
        else:
            state["requests"][0]["requested_at"] = "2026-10-29T10:00:00"
        path.write_text(json.dumps(state))
    restarted = _ledger(tmp_path)
    restarted.initialize(NOW)
    assert _reserve(restarted).reason == "budget_state_unverified"
    assert _reserve(restarted, NOW + timedelta(days=1)).allowed
    restarted.close()


@pytest.mark.parametrize("after_replace", [False, True])
def test_unacknowledged_write_never_grants_http_permission(tmp_path, monkeypatch, after_replace):
    ledger = _ready(tmp_path)
    original = budget_file.AtomicWriteJson

    def fail(path, payload):
        if after_replace:
            original(path, payload)
        raise OSError("fixture fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(budget_file, "AtomicWriteJson", fail)
        assert not _reserve(ledger).allowed
    assert _reserve(ledger).reason == "budget_state_unwritable"
    assert _reserve(ledger, NOW + timedelta(days=1)).allowed
    ledger.close()


def test_deleted_file_during_operation_stops_requests_and_status_stays_in_memory(tmp_path):
    ledger = _ready(tmp_path)
    assert _reserve(ledger).allowed
    (tmp_path / "runtime/state.json").unlink()
    assert ledger.summary(NOW.date().isoformat())["used"] == 1
    assert _reserve(ledger).reason == "budget_state_unwritable"
    assert not _reserve(ledger).allowed
    ledger.close()


def test_status_does_not_wait_for_writer_io(tmp_path, monkeypatch):
    ledger = _ready(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = budget_file.AtomicWriteJson

    def slow(path, payload):
        entered.set()
        assert release.wait(5)
        original(path, payload)

    monkeypatch.setattr(budget_file, "AtomicWriteJson", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_reserve, ledger)
        assert entered.wait(5)
        try:
            assert ledger.summary(NOW.date().isoformat())["used"] == 0
        finally:
            release.set()
        assert future.result(5).allowed
    ledger.close()


def _competing_process(root, send):
    ledger = _ledger(Path(root), runtime="other")
    ledger.initialize(NOW)
    send.send(_reserve(ledger).reason)
    ledger.close()
    send.close()


def test_same_data_root_excludes_a_second_process_with_different_runtime(tmp_path):
    ledger = _ready(tmp_path)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_competing_process, args=(str(tmp_path), send))
    process.start()
    send.close()
    try:
        assert receive.poll(10)
        assert receive.recv() == "budget_owner_unavailable"
        process.join(10)
        assert process.exitcode == 0
        assert not (tmp_path / "other/state.json").exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        receive.close()
        ledger.close()


def test_changing_runtime_and_returning_to_old_file_cannot_restore_today_budget(tmp_path):
    ledger = _ready(tmp_path)
    assert _reserve(ledger).allowed
    ledger.close()
    other = _ledger(tmp_path, runtime="other")
    other.initialize(NOW)
    assert _reserve(other).reason == "budget_state_unverified"
    assert _reserve(other, NOW + timedelta(days=1)).allowed
    other.close()
    previous = _ledger(tmp_path)
    previous.initialize(NOW + timedelta(days=1))
    assert not _reserve(previous, NOW + timedelta(days=1)).allowed
    previous.close()


def test_cross_day_completion_keeps_original_charge_and_cannot_trip_new_day(tmp_path):
    policy = DeepSeekHealthPolicy(1, 2, 0.2, 0.5, 2, 60)
    ledger = _ready(tmp_path, policy=policy)
    late = NOW.replace(hour=23, minute=59)
    batch = _batch(ledger, late)
    original = _reserve(ledger, late, batch_id=batch)
    tomorrow = late + timedelta(minutes=2)
    newer = _reserve(ledger, tomorrow)
    assert original.allowed and newer.allowed
    ledger.finish(original.reservation_id, status="failed", completed_at=tomorrow)
    ledger.finish_batch(BudgetBatchCompletion(batch, "failed", tomorrow, {}, 1))
    assert ledger.summary(late.date().isoformat())["used"] == 1
    current = ledger.summary(tomorrow.date().isoformat())
    assert current["used"] == 1 and current["health"]["mode"] == "closed"
    state = json.loads((tmp_path / "runtime/state.json").read_text())
    assert next(item for item in state["requests"] if item["request_id"] == original.reservation_id)["budget_date"] == late.date().isoformat()
    ledger.close()


def test_half_open_probe_is_exclusive_and_restart_reopens_abandoned_probe(tmp_path):
    policy = DeepSeekHealthPolicy(2, 5, 0.2, 0.5, 2, 60)
    ledger = _ready(tmp_path, policy=policy)
    for _ in range(2):
        _complete(ledger, _batch(ledger))
    assert _reserve(ledger).reason == "circuit_open"
    after = NOW + timedelta(seconds=61)
    probe = _batch(ledger, after)
    assert _reserve(ledger, after, batch_id=probe, candidate_count=1).allowed
    assert _reserve(ledger, after, batch_id=_batch(ledger, after), candidate_count=1).reason == "circuit_open"
    ledger.close()
    restarted = _ledger(tmp_path, policy=policy)
    restarted.initialize(after)
    assert _reserve(restarted, after).reason == "circuit_open"
    resumed = after + timedelta(seconds=61)
    _complete(restarted, _batch(restarted, resumed), resumed, success=True)
    assert restarted.summary(NOW.date().isoformat())["health"]["mode"] == "recovering"
    restarted.close()


@pytest.mark.parametrize("mode", ["schema", "application_ratio"])
def test_successful_transport_can_still_trip_schema_or_application_health(tmp_path, mode):
    policy = DeepSeekHealthPolicy(2 if mode == "schema" else 3, 2, 0.5, 0.5, 2, 60)
    ledger = _ready(tmp_path, policy=policy)
    for _ in range(2):
        batch = _batch(ledger)
        call = _reserve(ledger, batch_id=batch)
        ledger.finish(call.reservation_id, status="success", completed_at=NOW)
        ledger.finish_batch(BudgetBatchCompletion(batch, "failed" if mode == "schema" else "partial", NOW, {}, 1))
    expected = "consecutive_schema_failures" if mode == "schema" else "low_application_ratio"
    assert ledger.summary(NOW.date().isoformat())["health"]["reason"] == expected
    assert _reserve(ledger).reason == "circuit_open"
    ledger.close()


def test_late_completion_cannot_write_after_owner_handoff(tmp_path):
    ledger = _ready(tmp_path)
    call = _reserve(ledger)
    ledger.close()
    restarted = _ledger(tmp_path)
    restarted.initialize(NOW)
    assert _reserve(restarted).allowed
    path = tmp_path / "runtime/state.json"
    before = path.read_bytes()
    ledger.finish(call.reservation_id, status="success", completed_at=NOW)
    assert path.read_bytes() == before
    assert restarted.summary(NOW.date().isoformat())["used"] == 2
    restarted.close()


def test_backward_clock_and_expired_batch_cannot_reserve(tmp_path):
    ledger = _ready(tmp_path)
    assert _reserve(ledger, NOW - timedelta(days=1)).reason == "budget_clock_reversed"
    batch = _batch(ledger)
    assert _reserve(ledger, NOW + timedelta(minutes=2), batch_id=batch).reason == "batch_expired"
    ledger.close()


def test_skipped_batches_are_bounded_and_disabled_owner_creates_no_files(tmp_path):
    ledger = _ledger(tmp_path, enabled=False)
    ledger.initialize(NOW)
    for _ in range(500):
        batch = _batch(ledger)
        ledger.finish_batch(BudgetBatchCompletion(batch, "skipped", NOW, {}, 0))
    assert _reserve(ledger).reason == "disabled"
    assert ledger.summary(NOW.date().isoformat())["batch_status"]["skipped"] <= 168
    assert not tuple(tmp_path.iterdir())
    ledger.close()


def test_oversize_candidate_is_not_trimmed_to_free_quota(tmp_path, monkeypatch):
    ledger = _ready(tmp_path)
    monkeypatch.setattr(budget_file, "MAX_STATE_BYTES", 32)
    assert _reserve(ledger).reason == "budget_state_unwritable"
    assert ledger.summary(NOW.date().isoformat())["used"] == 1
    ledger.close()


def test_posix_atomic_write_syncs_contents_then_replacement_directory(tmp_path, monkeypatch):
    import os
    import stat
    from trader.infra.atomic_files import json as atomic_json

    if os.name == "nt":
        pytest.skip("POSIX directory sync")
    calls = []
    real_sync, real_replace = os.fsync, os.replace

    def sync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "contents")
        real_sync(descriptor)

    def rename(source, target):
        calls.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(atomic_json.os, "fsync", sync)
    monkeypatch.setattr(atomic_json.os, "replace", rename)
    AtomicWriteJson(tmp_path / "state.json", {"reserved": True})
    assert calls == ["contents", "replace", "directory"]


def test_atomic_directory_sync_failure_is_reported_after_replace(tmp_path, monkeypatch):
    from trader.infra.atomic_files import json as atomic_json

    monkeypatch.setattr(atomic_json, "SyncDirectory", lambda _path: (_ for _ in ()).throw(OSError("sync failed")))
    with pytest.raises(OSError, match="sync failed"):
        AtomicWriteJson(tmp_path / "state.json", {"reserved": True})
    assert json.loads((tmp_path / "state.json").read_text())["reserved"] is True
    assert tuple(tmp_path.glob(".state.json.*")) == ()


def test_windows_replacement_requests_write_through_and_propagates_failure(tmp_path, monkeypatch):
    import ctypes
    from unittest.mock import Mock
    from trader.infra.atomic_files import json as atomic_json

    move = Mock(return_value=1)
    kernel = Mock(MoveFileExW=move)
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "fixture denied"), raising=False)
    destination = tmp_path / "state.json"
    atomic_json._replace_windows("temporary", destination)
    move.assert_called_once_with("temporary", str(destination), 0x1 | 0x8)
    move.return_value = 0
    with pytest.raises(OSError, match="fixture denied"):
        atomic_json._replace_windows("temporary", destination)


def test_reviewer_sends_http_only_after_durable_reservation(tmp_path, monkeypatch):
    from tests.component.test_deepseek import FakeHttpResponse, _candidate_with_evidence, _reviewer_policy, _settings, _valid_payload
    from trader.recommendation.infra.deepseek.cache import ReviewCache
    from trader.recommendation.infra.deepseek.client import DeepSeekHttpClient
    from trader.recommendation.infra.deepseek.reviewer import DeepSeekReviewer

    ledger = _ready(tmp_path, limit=2)
    candidate = _candidate_with_evidence()
    observed = []

    def post(*_args, **_kwargs):
        state = json.loads((tmp_path / "runtime/state.json").read_text())
        observed.append(state["requests"][-1]["status"])
        return FakeHttpResponse(200, {"choices": [{"message": {"content": json.dumps(_valid_payload(candidate.quote.code))}}]})

    reviewer = DeepSeekReviewer(
        _settings(), ledger, DeepSeekHttpClient(post=post), ReviewCache(), now=lambda: NOW, **_reviewer_policy(),
    )
    reviewer.review(Strategy.TOMORROW, (candidate,), phase="morning_main", deadline=NOW + timedelta(minutes=1))
    assert observed == ["reserved"]
    ledger.close()

    fresh = _ready(tmp_path / "failed", limit=2)
    reviewer = DeepSeekReviewer(
        _settings(), fresh, DeepSeekHttpClient(post=post), ReviewCache(), now=lambda: NOW, **_reviewer_policy(),
    )
    monkeypatch.setattr(budget_file, "AtomicWriteJson", lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")))
    reviewer.review(Strategy.TOMORROW, (candidate,), phase="morning_main", deadline=NOW + timedelta(minutes=1))
    assert observed == ["reserved"]
    assert reviewer.status()["physical_call_acceptance"]["zero_call_reason"] == "budget_state_unwritable"
    fresh.close()

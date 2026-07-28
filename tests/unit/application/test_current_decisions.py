from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.unit.domain.test_tomorrow_fusion import _evaluation, _request, _selection
from trader.application.current_decisions import CurrentDecisionIndex
from trader.domain.recommendation.tomorrow_fusion import build_tomorrow_decision_epoch

SHANGHAI = ZoneInfo("Asia/Shanghai")
BOUNDARY = datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI)


def test_current_decision_index_requires_expected_version_cas() -> None:
    index = CurrentDecisionIndex()
    first = _decision(1, BOUNDARY - timedelta(minutes=2))
    second = _decision(2, BOUNDARY - timedelta(minutes=1))

    assert index.publish(first, expected_current_version=None).accepted is True

    stale_cas = index.publish(second, expected_current_version=None)
    accepted = index.publish(second, expected_current_version=first.version)

    assert stale_cas.accepted is False
    assert stale_cas.reason == "cas_mismatch"
    assert accepted.accepted is True
    assert index.latest() == second


def test_current_decision_index_allows_only_one_concurrent_cas_winner() -> None:
    index = CurrentDecisionIndex()
    first = _decision(1, BOUNDARY - timedelta(minutes=2))
    index.publish(first, expected_current_version=None)
    candidates = (
        _decision(2, BOUNDARY - timedelta(seconds=50)),
        replace(
            _decision(2, BOUNDARY - timedelta(seconds=50)),
            degraded_reasons=("source_delay",),
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda candidate: index.publish(
                    candidate,
                    expected_current_version=first.version,
                ),
                candidates,
            )
        )

    assert sum(result.accepted for result in results) == 1
    assert {result.reason for result in results} == {"accepted", "cas_mismatch"}


def test_freeze_seal_uses_checkpoint_when_current_decision_is_after_boundary() -> None:
    index = CurrentDecisionIndex()
    checkpoint_decision = _decision(1, BOUNDARY - timedelta(seconds=10))
    late_decision = _decision(2, BOUNDARY + timedelta(seconds=1))
    index.publish(late_decision, expected_current_version=None)

    seal = index.seal_for_freeze(
        boundary_at=BOUNDARY,
        fallback_decision=checkpoint_decision,
    )
    rejected = index.publish(
        _decision(3, BOUNDARY + timedelta(seconds=2)),
        expected_current_version=late_decision.version,
    )

    assert seal.accepted is True
    assert seal.decision == checkpoint_decision
    assert seal.source == "fallback"
    assert rejected.accepted is False
    assert rejected.reason == "freeze_sealed"
    assert index.latest() == late_decision


def test_hybrid_publish_must_reference_the_current_local_parent() -> None:
    index = CurrentDecisionIndex()
    local = _decision(1, BOUNDARY - timedelta(minutes=2))
    index.publish(local, expected_current_version=None)
    invalid_hybrid = replace(
        _decision(2, BOUNDARY - timedelta(minutes=1)),
        projection_stage="hybrid",
        parent_decision_version="decision:wrong-parent",
    )

    result = index.publish(
        invalid_hybrid,
        expected_current_version=local.version,
    )

    assert result.accepted is False
    assert result.reason == "parent_mismatch"
    assert index.latest() == local


def _decision(sequence: int, observed_at: datetime):
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(3))
    request = replace(
        _request(_selection(evaluations)),
        sequence=sequence,
        observed_at=observed_at,
    )
    return build_tomorrow_decision_epoch(request)

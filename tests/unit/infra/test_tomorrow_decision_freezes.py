from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.domain.test_tomorrow_fusion import (
    _evaluation,
    _request,
    _review,
    _selection,
)
from trader.application.ports.decision_freezes import (
    DecisionFreezeConflictError,
    DecisionFreezeUnavailableError,
)
from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
    build_decision_anchors,
)
from trader.domain.recommendation.tomorrow_fusion import build_tomorrow_decision_epoch
from trader.infra.persistence.tomorrow_decision_freezes import (
    TomorrowDecisionFreezeRepository,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BOUNDARY = datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI)


def test_checkpoint_round_trip_and_consumption_are_manifest_verified(tmp_path: Path) -> None:
    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()
    checkpoint = TomorrowFreezeCheckpoint(
        decision=_decision(1, BOUNDARY - timedelta(seconds=10)),
        boundary_at=BOUNDARY,
    )

    repository.save_checkpoint(checkpoint)
    loaded = repository.load_checkpoint(checkpoint.trade_date)
    repository.consume_checkpoint(checkpoint.version, consumed_at=BOUNDARY)

    assert loaded == checkpoint
    assert repository.load_checkpoint(checkpoint.trade_date) is None
    assert (tmp_path / "tomorrow-v2" / "tomorrow-v2.sqlite3").is_file()


def test_formal_freeze_round_trip_is_idempotent_and_rejects_same_day_conflict(
    tmp_path: Path,
) -> None:
    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()
    first = _freeze(_decision(1, BOUNDARY - timedelta(seconds=1)))
    conflict = _freeze(
        replace(
            _decision(2, BOUNDARY - timedelta(seconds=1)),
            degraded_reasons=("source_delay",),
        )
    )

    repository.commit_freeze(first)
    repository.commit_freeze(first)

    assert repository.load_frozen(first.trade_date) == first
    with pytest.raises(DecisionFreezeConflictError, match="already committed"):
        repository.commit_freeze(conflict)


def test_corrupt_formal_file_fails_closed_with_typed_unavailable_error(
    tmp_path: Path,
) -> None:
    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()
    frozen = _freeze(_decision(1, BOUNDARY - timedelta(seconds=1)))
    repository.commit_freeze(frozen)
    payload_path = next((tmp_path / "tomorrow-v2" / "freezes").rglob("*.json"))
    payload_path.write_bytes(b"corrupt")

    with pytest.raises(DecisionFreezeUnavailableError, match="verification"):
        repository.load_frozen(frozen.trade_date)


def test_hybrid_freeze_round_trip_preserves_review_and_shanghai_time(
    tmp_path: Path,
) -> None:
    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()
    frozen = _freeze(_hybrid_decision())

    repository.commit_freeze(frozen)
    loaded = repository.load_frozen(frozen.trade_date)

    assert loaded == frozen
    assert loaded is not None
    assert loaded.decision.projection_stage == "hybrid"
    assert loaded.decision.entries[1].review is not None
    assert getattr(loaded.decision.observed_at.tzinfo, "key", None) == "Asia/Shanghai"


def _freeze(decision) -> TomorrowDecisionFreeze:
    return TomorrowDecisionFreeze(
        decision=decision,
        frozen_at=BOUNDARY,
        freeze_kind="scheduled",
        anchors=build_decision_anchors(decision),
    )


def _decision(sequence: int, observed_at: datetime):
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(3))
    request = replace(
        _request(_selection(evaluations)),
        sequence=sequence,
        observed_at=observed_at,
    )
    return build_tomorrow_decision_epoch(request)


def _hybrid_decision():
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(3))
    request = replace(
        _request(
            _selection(evaluations),
            reviews={"600001": _review("600001", 92.0)},
            projection_stage="hybrid",
            review_candidate_codes=("600001",),
            parent_decision_version="decision:local",
        ),
        sequence=2,
        observed_at=BOUNDARY - timedelta(seconds=1),
    )
    return build_tomorrow_decision_epoch(request)

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.unit.domain.test_tomorrow_fusion import _evaluation, _request, _selection
from trader.domain.recommendation.tomorrow_freeze import (
    DecisionAnchor,
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
    build_decision_anchors,
)
from trader.domain.recommendation.tomorrow_fusion import build_tomorrow_decision_epoch

SHANGHAI = ZoneInfo("Asia/Shanghai")
BOUNDARY = datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI)


def test_checkpoint_requires_a_current_decision_within_thirty_seconds() -> None:
    current = _decision(1, BOUNDARY - timedelta(seconds=10))

    checkpoint = TomorrowFreezeCheckpoint(decision=current, boundary_at=BOUNDARY)

    assert checkpoint.decision_version == current.version
    assert checkpoint.content_hash
    assert checkpoint.version.startswith("tomorrow-checkpoint:2026-07-28:")

    with pytest.raises(ValueError, match="within 30 seconds"):
        TomorrowFreezeCheckpoint(
            decision=_decision(2, BOUNDARY - timedelta(seconds=31)),
            boundary_at=BOUNDARY,
        )


def test_scheduled_freeze_anchors_exactly_the_selected_decisions() -> None:
    decision = _decision(1, BOUNDARY - timedelta(seconds=1))
    anchors = build_decision_anchors(decision)

    frozen = TomorrowDecisionFreeze(
        decision=decision,
        frozen_at=BOUNDARY,
        freeze_kind="scheduled",
        anchors=anchors,
    )

    assert tuple(anchor.code for anchor in frozen.anchors) == tuple(
        item.code for item in decision.entries if item.selected
    )
    assert frozen.version.startswith("tomorrow-freeze:2026-07-28:")
    assert replace(frozen).content_hash == frozen.content_hash

    with pytest.raises(ValueError, match="selected decision codes"):
        replace(frozen, anchors=())


def test_close_fallback_requires_official_close_reasons_and_valid_anchor_time() -> None:
    observed_at = BOUNDARY + timedelta(minutes=11)
    decision = _decision(3, observed_at)
    anchors = tuple(
        replace(anchor, source="official_close", source_time=BOUNDARY + timedelta(minutes=10))
        for anchor in build_decision_anchors(decision)
    )

    frozen = TomorrowDecisionFreeze(
        decision=decision,
        frozen_at=observed_at,
        freeze_kind="close_fallback",
        anchors=anchors,
        degraded_reasons=("close_fallback", "official_close", "local_only"),
    )

    assert frozen.freeze_kind == "close_fallback"
    assert frozen.degraded_reasons == ("close_fallback", "local_only", "official_close")

    future_anchor = replace(anchors[0], source_time=observed_at + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future anchor"):
        replace(frozen, anchors=(future_anchor, *anchors[1:]))


def test_decision_anchor_rejects_non_positive_prices() -> None:
    with pytest.raises(ValueError, match="positive"):
        DecisionAnchor(
            code="600001",
            price=0.0,
            pct_change=0.0,
            source="official_close",
            source_time=BOUNDARY,
            data_version="close-v1",
        )


def _decision(sequence: int, observed_at: datetime):
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(3))
    request = replace(
        _request(_selection(evaluations)),
        sequence=sequence,
        observed_at=observed_at,
    )
    return build_tomorrow_decision_epoch(request)

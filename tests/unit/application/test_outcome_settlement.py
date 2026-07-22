from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.application.outcome_settlement import OutcomeSettlementService
from trader.domain.models import Strategy
from trader.domain.outcomes import BenchmarkReturn, OutcomeBar, OutcomeTarget

NOW = datetime(2026, 7, 21, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


class _History:
    def read_outcome_bars(self, codes, observed_at):
        assert tuple(codes) == ("600001",)
        assert observed_at == NOW
        return {
            "600001": (
                OutcomeBar("2026-07-20", 10.0, 10.1, 9.9, 10.0, 0.0),
                OutcomeBar("2026-07-21", 10.0, 10.3, 9.6, 10.2, 2.0),
            ),
        }


class _Repository:
    def __init__(self) -> None:
        self.saved = ()
        self.benchmark = ()

    def pending_outcome_targets(self, *, limit):
        assert limit == 500
        return (OutcomeTarget("snapshot", Strategy.TOMORROW, "2026-07-20", "600001", 10.0, 2.0),)

    def record_benchmark_return(self, benchmark, *, observed_at):
        self.benchmark = (benchmark, observed_at)

    def benchmark_returns_after(self, recommend_date, *, limit):
        assert recommend_date == "2026-07-20"
        assert limit == 1
        return (BenchmarkReturn("2026-07-21", 0.5),)

    def save_recommendation_outcomes(self, outcomes):
        self.saved = tuple(outcomes)


def test_after_close_settlement_records_benchmark_and_due_outcome(application_feature_factory) -> None:
    repository = _Repository()
    feature = application_feature_factory("600001", NOW)
    peer = application_feature_factory("600002", NOW)
    service = OutcomeSettlementService(
        _History(),
        repository,
        session_distance=lambda start, end: (date.fromisoformat(end) - date.fromisoformat(start)).days,
    )

    result = service.settle(
        NOW,
        (feature, replace(peer, quote=replace(peer.quote, pct_change=1.0))),
    )

    assert result.target_count == 1
    assert result.completed_count == 1
    assert repository.benchmark == (BenchmarkReturn("2026-07-21", 2.0), NOW)
    assert len(repository.saved) == 1
    assert repository.saved[0].net_excess_return_pct == pytest.approx(1.3)


def test_settlement_skips_horizons_that_are_not_due(application_feature_factory) -> None:
    repository = _Repository()
    service = OutcomeSettlementService(
        _History(),
        repository,
        session_distance=lambda _start, _end: 0,
    )

    result = service.settle(NOW, (application_feature_factory("600001", NOW),))

    assert result.completed_count == 0
    assert repository.saved == ()

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.unit.test_market_data_cache import _identity, _policy
from trader.infra.cache import BoundedLruCache
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.budget_file import BudgetStateFile
from trader.infra.process_lock import ProcessLock
from trader.recommendation.infra.deepseek.health_gate import _half_open_allowed
from trader.recommendation.infra.deepseek.reviewer_selection import _ReservationTracker

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    "hour,minute,stage",
    [(9, 20, "morning"), (12, 15, "morning"), (13, 30, "afternoon"), (14, 47, "final"), (14, 59, "final")],
)
def test_continuous_review_keeps_existing_budget_buckets_until_close(tmp_path, hour, minute, stage):
    at = datetime(2026, 7, 16, hour, minute, tzinfo=_SHANGHAI)
    ledger = DeepSeekBudgetLedger(
        BudgetStateFile(tmp_path / "state.json", ProcessLock(tmp_path / ".deepseek.lock", label="DeepSeek")),
        daily_hard_limit=168,
        strategy_limits={"tomorrow": 6, "d25": 0, "shared_preheat": 0, "emergency": 0},
        stage_targets={"tomorrow_" + name: 0 for name in ("morning", "afternoon", "final")},
        stage_limits={"tomorrow_" + name: 2 for name in ("morning", "afternoon", "final")},
    )
    ledger.initialize(at - timedelta(days=1))
    ledger.initialize(at)
    reserved = ledger.reserve(Strategy.TOMORROW, phase="before_close", requested_at=at)
    assert reserved.allowed and reserved.stage == "tomorrow_" + stage
    assert _half_open_allowed(at)
    close = at.replace(hour=15, minute=0)
    assert not _half_open_allowed(close)
    tracker = _ReservationTracker(
        budget=ledger,
        strategy=Strategy.TOMORROW,
        phase="before_close",
        deadline=close,
        now=lambda: close,
        planned_bucket="tomorrow",
        batch_id="at-close",
        emergency_reason="",
        model_role="primary",
        requested_model="fixture",
        reasoning_effort="",
    )
    assert not tracker.reserve()
    assert tracker.failure_reason == "deadline_reached"


def test_after_close_cache_uses_ten_second_quote_ttl_with_separate_identity():
    at = datetime(2026, 7, 16, 22, tzinfo=_SHANGHAI)
    cache = BoundedLruCache(
        _policy(),
        cadence_seconds={"full_market": {"before_close": 10}},
        wall_clock=lambda: at,
    )
    closing = _identity("600001", dataset="full_market_quotes", phase="after_close")
    intraday = _identity("600001", dataset="full_market_quotes", phase="before_close")
    assert closing != intraday
    assert cache.put(closing, "close", data_version="close", source_time=at)
    assert cache.is_actionable(closing, at - timedelta(seconds=29))
    assert not cache.is_actionable(closing, at - timedelta(seconds=31))

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tests.unit.application.test_today_v2_projection import _features, _native_input
from tests.unit.application.test_tomorrow_deepseek_fusion import _review
from tests.unit.domain.test_decision_identity import decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_observers import AsyncDecisionObserver
from trader.application.today_v2_freezing import TodayV2FreezeCoordinator
from trader.application.today_v2_projection import build_today_v2_local
from trader.application.today_v2_runtime import TodayV2Runtime, TodayV2RuntimeDependencies
from trader.application.tomorrow_v2_freezing import V2DecisionRuntimeIdentity
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import MarketQuote
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository
from trader.infra.settings import load_strategy_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)


def test_formal_today_accepts_only_matching_quote_overlay_without_mutating_decision(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    clock = _Clock(_at(11, 20))
    index = UnifiedDecisionIndex()
    fixture = decision(Strategy.TODAY)
    anchor = fixture.items[0].quote
    assert anchor is not None
    current = replace(
        fixture,
        observed_at=_at(11, 19, 59),
        items=(replace(fixture.items[0], quote=replace(anchor, source_time=_at(11, 19, 59))),),
    )
    assert index.publish(current, expected_version=None).accepted
    freezer = TodayV2FreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity("config-v1", "strategy-v1", "fusion-v1"),
    )
    frozen = freezer.freeze_scheduled()
    assert frozen.record is not None
    runtime = TodayV2Runtime(
        None,
        TodayV2RuntimeDependencies(
            reviewer=None,
            index=index,
            observer=AsyncDecisionObserver((), capacity=4),
            freezer=freezer,
            clock=clock,
        ),
    )
    formal_before = index.snapshot(Strategy.TODAY).formal
    quote = MarketQuote(
        code="600001",
        name="测试",
        price=10.5,
        previous_close=10.0,
        open_price=10.1,
        high=10.6,
        low=10.0,
        pct_change=5.0,
        change_5m=None,
        speed=None,
        volume_ratio=None,
        turnover_rate=None,
        amount=None,
        amplitude=None,
        market_cap=None,
        industry="测试",
        source="fixture",
        source_time=_at(14, 0),
        received_time=_at(14, 0),
        data_version="quote-v2",
    )

    assert runtime.overlay_codes(clock.value.date()) == ("600001",)
    assert runtime.publish_overlay({quote.code: quote}, observed_at=_at(14, 0), closing=False)
    snapshot = index.snapshot(Strategy.TODAY)
    assert snapshot.formal == formal_before
    assert snapshot.current == formal_before.decision if formal_before is not None else False
    assert snapshot.overlay is not None and snapshot.overlay.parent_version == formal_before.decision.version
    assert not runtime.publish_overlay({"600002": replace(quote, code="600002")}, observed_at=_at(14, 1), closing=False)
    utc_quote = replace(
        quote,
        source_time=_at(14, 2).astimezone(timezone.utc),
        received_time=_at(14, 2).astimezone(timezone.utc),
        data_version="quote-v3",
    )
    assert runtime.publish_overlay(
        {utc_quote.code: utc_quote},
        observed_at=_at(14, 2).astimezone(timezone.utc),
        closing=False,
    )


def test_review_returning_after_boundary_freezes_local_and_cannot_publish_hybrid(
    application_feature_factory,
    tmp_path: Path,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    native_input = _native_input(_features(application_feature_factory))
    projection = build_today_v2_local(native_input, policy, sequence=1)
    boundary = native_input.evaluated_at.replace(hour=11, minute=20, second=0, microsecond=0)
    before_submit_cutoff = boundary.replace(minute=17)
    clock = _Clock(before_submit_cutoff)
    index = UnifiedDecisionIndex()
    assert index.publish(projection.local, expected_version=None).accepted
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    freezer = TodayV2FreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity(
            native_input.config_version,
            policy.strategy_version,
            policy.fusion_version,
        ),
    )

    class LateReturningReviewer:
        @staticmethod
        def evidence_manifest_hash(candidate) -> str:
            return f"manifest:{candidate.quote.code}"

        def review(self, _strategy, candidates, *, phase, deadline, contexts=None):
            del phase, deadline, contexts
            clock.value = boundary
            code = candidates[0].quote.code
            return {
                code: replace(
                    _review(code, 100.0),
                    completed_at=boundary.replace(second=0) - timedelta(seconds=1),
                    evidence_manifest_hash=f"manifest:{code}",
                )
            }

    runtime = TodayV2Runtime(
        policy,
        TodayV2RuntimeDependencies(
            LateReturningReviewer(),
            index,
            AsyncDecisionObserver((), capacity=4),
            freezer,
            clock,
        ),
    )
    before_cutoff = SimpleNamespace(
        trade_date=native_input.trade_date,
        evaluated_at=before_submit_cutoff,
        phase=native_input.phase,
    )

    runtime._try_hybrid_upgrade(before_cutoff, projection, projection.local)

    snapshot = index.snapshot(Strategy.TODAY)
    assert snapshot.formal is not None and snapshot.current == snapshot.formal.decision
    assert runtime.status().hybrid_publish_count == 0
    assert runtime.status().review_late_count == 1

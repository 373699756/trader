from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    FreezeRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import DeepSeekAdapter
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord, FormalScoredFreeze
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _shadow(observed_at: datetime):
    source = freeze()
    return replace(
        source,
        observed_at=observed_at,
        input_versions=(
            ("score_scale", "learned_rank_percentile_0_100"),
            ("score_model_id", "learned_rank"),
            ("score_model", "learned_rank:" + "a" * 64),
            ("score_contract_hash", "b" * 64),
            ("rank_population", "learned-rank-population:" + "c" * 64),
            ("execution_mode", "shadow"),
            ("production_authority", "false"),
        ),
        items=(replace(source.items[0], action=RecommendationAction.OBSERVE, final_score=100.0, quote=None),),
    )


@pytest.mark.parametrize("hour,minute", ((10, 0), (12, 30), (14, 59), (15, 0), (15, 10)))
@pytest.mark.parametrize("cold", (False, True))
def test_shadow_never_checkpoints_freezes_or_enters_official_history(
    tmp_path: Path, hour: int, minute: int, cold: bool
) -> None:
    now = NOW.replace(hour=hour, minute=minute)
    shadow = _shadow(now)
    records = SQLiteFreezeRecords(tmp_path / "v1/freeze.sqlite3", sqlite_policy=SqlitePolicyFixture())
    records.initialize()
    index = UnifiedFreezeIndex()
    if not cold:
        assert index.publish(shadow, expected_version=None).accepted
    freezer = ScoredFreezeCoordinator(
        index,
        records,
        _Clock(now),
        runtime_identity=FreezeRuntimeIdentity(shadow.config_version, shadow.strategy_version, shadow.fusion_version),
    )
    assert freezer.restore(now.date()).status == "formal_freeze_unavailable"
    if now.hour < 15:
        assert freezer.freeze_scheduled().status == "before_freeze"
    elif not cold:
        assert freezer.freeze_scheduled().status == "shadow_only"
    assert freezer.freeze_close_fallback(
        shadow, recovery_path="close_rebuild", official_close_version="close"
    ).status == ("before_close_recovery" if now.hour < 15 else "shadow_only")
    assert records.load(Strategy.TOMORROW, now.date()) is None
    assert records.list_dates(Strategy.TOMORROW) == ()
    with pytest.raises(ValueError, match="shadow"):
        FormalScoredFreeze(shadow)
    with pytest.raises(ValueError, match="shadow"):
        CommittedFreezeRecord(shadow, now, "scheduled")


def test_shadow_adapter_skips_review_before_touching_any_production_dependency() -> None:
    # A Shadow lane must contribute zero physical requests to the shared 168 budget.
    adapter = DeepSeekAdapter(None, None, None, now=lambda: NOW)
    assert adapter.build_hybrid(_shadow(NOW), None) is None


def test_learned_rank_identity_cannot_grant_authority_or_executable_action() -> None:
    shadow = _shadow(NOW)
    with pytest.raises(ValueError, match="shadow identity"):
        replace(
            shadow,
            input_versions=tuple(
                (key, "true" if key == "production_authority" else value) for key, value in shadow.input_versions
            ),
        )
    with pytest.raises(ValueError, match="shadow identity"):
        replace(shadow, items=(replace(shadow.items[0], action=RecommendationAction.EXECUTABLE),))

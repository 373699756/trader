from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.unit.domain.test_decision_identity import decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.tomorrow_v2_freezing import (
    TomorrowV2FreezeCoordinator,
    V2DecisionRuntimeIdentity,
)
from trader.application.tomorrow_v2_views import UnifiedTomorrowDecisionQueries
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 11, hour, minute, tzinfo=SHANGHAI)


def test_current_hides_unfrozen_draft_at_boundary_and_exposes_only_formal_record(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    draft = replace(decision(), observed_at=_at(14, 49))
    assert index.publish(draft, expected_version=None).accepted
    clock = _Clock(_at(14, 50))
    queries = UnifiedTomorrowDecisionQueries(index, repository, clock)

    assert queries.current().status == "not_ready"
    assert queries.status().status == "not_ready"

    freezer = TomorrowV2FreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity("config-v1", "strategy-v1", "fusion-v1"),
    )
    assert freezer.freeze_scheduled().status == "frozen"

    current = queries.current()
    assert current.status == "ready"
    assert current.frozen
    assert current.decision_version == index.snapshot(draft.strategy).formal.decision.version

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.domain.test_decision_identity import decision
from tests.unit.domain.test_preview_identity import STARTED_AT, preview_decision
from trader.recommendation.application.pipeline.freeze_publish.decision_events import (
    BuildDecisionCommitted,
    DecisionObservation,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    DecisionRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.decision_records import DecisionCheckpoint
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords
from trader.training.infra.research.research_trace_index import SQLiteResearchTraceIndex


def test_preview_writer_guards_reject_before_creating_any_database(tmp_path) -> None:
    current = preview_decision()
    live = decision()
    record = CommittedDecisionRecord(live, live.observed_at, "scheduled")
    checkpoint = DecisionCheckpoint(live, live.observed_at.replace(hour=15))
    # Exercise adapter guards even if an unvalidated object bypasses frozen domain constructors.
    object.__setattr__(record, "decision", current)
    object.__setattr__(checkpoint, "decision", current)
    root = tmp_path / "formal"
    records = SQLiteDecisionRecords(root / "decisions.sqlite3")
    with pytest.raises(ValueError, match="preview"):
        records.commit(record)
    with pytest.raises(ValueError, match="preview"):
        records.save_checkpoint(checkpoint)
    with pytest.raises(ValueError, match="preview"):
        records.consume_checkpoint(checkpoint, consumed_at=STARTED_AT)
    with pytest.raises(ValueError, match="preview"):
        CommittedRecordBytes(record)
    assert not root.exists()
    index = UnifiedDecisionIndex()
    assert not index.commit_formal(record)
    assert not index.restore_formal(record)


@pytest.mark.parametrize("with_projection", (True, False))
def test_preview_research_trace_rejects_before_initialization_or_audit_materialization(
    tmp_path, with_projection: bool
) -> None:
    event = BuildDecisionCommitted(preview_decision())
    if not with_projection:
        event = replace(event, projection=None)
    history_root = tmp_path / "history"
    history = SQLiteResearchTraceIndex(history_root)
    with pytest.raises(ValueError, match="preview"):
        history.record(DecisionObservation(event, None))
    assert not history_root.exists()


def test_preview_freeze_and_restore_do_not_read_or_write_formal_storage(tmp_path) -> None:
    current = preview_decision()
    index = UnifiedDecisionIndex()
    assert index.publish(current, expected_version=None).accepted
    root = tmp_path / "formal"

    class RejectRecords(SQLiteDecisionRecords):
        def load(self, strategy, trade_date):
            pytest.fail("preview accessed formal storage")

    class Clock:
        def now(self):
            return STARTED_AT.replace(hour=16)

    coordinator = ScoredFreezeCoordinator(
        index,
        RejectRecords(root / "decisions.sqlite3"),
        Clock(),
        runtime_identity=DecisionRuntimeIdentity(
            current.config_version, current.strategy_version, current.fusion_version
        ),
    )
    assert coordinator.freeze_scheduled().status == "preview_only"
    assert coordinator.restore(current.trade_date).status == "preview_only"
    assert (
        coordinator.freeze_close_fallback(
            current, recovery_path="current", official_close_version="official-close:test"
        ).status
        == "preview_only"
    )
    assert index.seal_for_freeze(Strategy.TOMORROW, boundary_at=STARTED_AT.replace(hour=15)).reason == "preview_only"
    assert (
        index.seal_close_fallback(
            current, boundary_at=STARTED_AT.replace(hour=15), official_close_version="official-close:test"
        ).reason
        == "preview_only"
    )
    assert index.snapshot(Strategy.TOMORROW).formal is None
    assert not root.exists()


def test_next_scored_sequence_reads_both_currents_without_creating_state() -> None:
    index = UnifiedDecisionIndex()
    assert index.next_scored_sequence == 1
    assert index.publish(decision(sequence=3), expected_version=None).accepted
    assert index.next_scored_sequence == 4
    assert index.publish(decision(Strategy.D25, sequence=8), expected_version=None).accepted
    assert index.next_scored_sequence == index.next_scored_sequence == 9

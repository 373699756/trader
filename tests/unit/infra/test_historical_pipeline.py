from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import json
from dataclasses import replace

import pytest

from tests.unit.domain.test_freeze_identity import NOW, freeze, pipeline, stage_snapshots
from trader.http_api.response.freeze_projection import SerializeEvent, SerializeFreezeView
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import SqliteSession
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import BuildFreezeCommitted
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput, StageOutput
from trader.recommendation.application.ports.freeze_records import FreezeRecordUnavailableError
from trader.recommendation.domain.evidence import pipeline as production_pipeline
from trader.recommendation.domain.publication import freeze_identity
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord, FormalScoredFreeze
from trader.recommendation.domain.publication.historical_pipeline import HistoricalPipelineAudit
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes, CommittedRecordFromBytes
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords

_HISTORICAL_STAGES = (
    "data_source",
    "static_market",
    "static_standardize",
    "static_filter",
    "dynamic_market",
    "dynamic_standardize",
    "dynamic_filter",
    "candidate_pool",
    "quality_check",
    "local_score",
    "risk_review",
    "score_merge",
    "downside_action",
    "final_selection",
)
_HISTORICAL_DIAGNOSTICS = (
    "input_readiness",
    "dynamic_filter",
    "board_cross_section",
    "strategy_history",
    "model_input",
    "candidate_score",
    "board_limit",
    "candidate_refresh",
    "input_coverage",
    "evidence_score",
    "model_cost_gate",
    "local_score",
    "deepseek_review",
    "fusion",
    "action_gate",
    "concentration",
)


def _resign(raw, *, legacy_identity=True):
    raw["freeze_hash"] = CanonicalDigest(raw["freeze"])
    if legacy_identity:
        identity = raw["freeze"]
        raw["freeze_version"] = (
            f"freeze:{identity['strategy']}:{identity['trade_date']}:{identity['stage']}:"
            f"{identity['sequence']}:{raw['freeze_hash'][:16]}"
        )
    raw["payload_hash"] = CanonicalDigest(
        {key: value for key, value in raw.items() if key not in {"payload_hash", "version"}}
    )
    if legacy_identity:
        raw["version"] = f"record:{raw['freeze']['strategy']}:{raw['freeze']['trade_date']}:{raw['payload_hash'][:16]}"
    return json.dumps(raw, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _historical_bytes(strategy=Strategy.TOMORROW, *, audit="full", legacy_identity=True):
    # Persisted literals deliberately do not depend on either audit implementation.
    raw = json.loads(CommittedRecordBytes(CommittedFreezeRecord(freeze(strategy), NOW, "scheduled")))
    if audit != "none":
        raw["freeze"]["pipeline"] = {
            "current_stage": "concentration",
            "stages": [
                {
                    "key": key,
                    "state": "completed",
                    "input_count": 1,
                    "output_count": 1,
                    "metric_ranges": [],
                    "threshold": None,
                    "facets": [],
                    "reason_counts": [],
                }
                for key in _HISTORICAL_DIAGNOSTICS
            ],
        }
        raw["freeze"]["pipeline"]["stages"][0].update(
            metric_ranges=[{"metric": "board_reliability", "minimum": 0.9, "maximum": 1.0}],
            threshold=0.85,
            facets=[{"key": "ready", "count": 1, "total": 1}],
            reason_counts=[{"reason": "fixture_evidence", "count": 1}],
        )
    if audit == "full":
        raw["freeze"]["pipeline"]["stage_snapshots"] = [
            {
                "stage": stage,
                "stage_order": index + 1,
                "input_batch_id": f"old-{index}",
                "output_batch_id": f"old-{index + 1}",
                "as_of": NOW.isoformat(),
                "state": "degraded",
                "input_count": 1,
                "output_count": 1,
                "rejected_count": 0,
                "pending_count": 0,
                "failed_count": 0,
                "reasons": [{"code": "source_partial", "label": "历史来源覆盖不足", "count": 1, "severity": "warning"}],
                "source_health": {
                    "state": "degraded",
                    "source_count": 2,
                    "healthy_source_count": 1,
                    "latest_success_at": NOW.isoformat(),
                    "age_seconds": 2.0,
                },
                "latency_ms": 12,
                "degraded": True,
            }
            for index, stage in enumerate(_HISTORICAL_STAGES)
        ]
    return _resign(raw, legacy_identity=legacy_identity)


def _disable_production_audit(monkeypatch):
    def rejected(*args, **kwargs):
        raise AssertionError("historical audit reached an active pipeline implementation")

    monkeypatch.setattr(production_pipeline, "PIPELINE_STAGES", production_pipeline.PIPELINE_STAGES[:11])
    monkeypatch.setattr(production_pipeline, "PIPELINE_STAGE_ORDER", ("different_active_diagnostic",))
    monkeypatch.setattr(production_pipeline.PipelineStageSnapshot, "__post_init__", rejected)
    monkeypatch.setattr(production_pipeline.RecommendationPipelineStatus, "__post_init__", rejected)
    monkeypatch.setattr(production_pipeline.SourceHealth, "__post_init__", rejected)
    monkeypatch.setattr(freeze_identity, "_pipeline_payload", rejected)
    monkeypatch.setattr(freeze_identity, "PipelineSnapshotIdentityPayload", rejected)


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("audit", ("full", "diagnostics", "none"))
@pytest.mark.parametrize("legacy_identity", (True, False))
def test_historical_bytes_and_identity_survive_production_pipeline_changes(
    monkeypatch, strategy, audit, legacy_identity
):
    encoded = _historical_bytes(strategy, audit=audit, legacy_identity=legacy_identity)
    before = json.loads(encoded)
    _disable_production_audit(monkeypatch)
    restored = CommittedRecordFromBytes(encoded)

    assert CommittedRecordBytes(restored) == encoded
    assert restored.version == before["version"]
    assert restored.payload_hash == before["payload_hash"]
    assert restored.freeze.version == before["freeze_version"]
    assert restored.freeze.content_hash == before["freeze_hash"]
    assert dict(restored.freeze.input_versions) == dict(before["freeze"]["input_versions"])
    if audit == "none":
        assert restored.freeze.pipeline is None
    else:
        assert isinstance(restored.freeze.pipeline, HistoricalPipelineAudit)
        assert len(restored.freeze.pipeline.stages) == 16
        assert len(restored.freeze.pipeline.stage_snapshots) == (14 if audit == "full" else 0)


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_historical_recovery_preserves_database_bytes_and_get_sse_projection(tmp_path, monkeypatch, strategy):
    class Clock:
        def now(self):
            return NOW

    encoded = _historical_bytes(strategy)
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", sqlite_policy=SqlitePolicyFixture())
    records.initialize()
    records.import_record_bytes(encoded)
    _disable_production_audit(monkeypatch)
    restarted = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", sqlite_policy=SqlitePolicyFixture())
    assert restarted.recover().quarantined == 0
    restored = restarted.load(strategy, NOW.date())
    assert restored is not None
    index = UnifiedFreezeIndex()
    assert index.restore_formal(restored)
    assert index.restore_formal(restored)
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), restarted, Clock())
    history = SerializeFreezeView(queries.history(strategy, NOW.date()))
    current = SerializeFreezeView(queries.current(strategy))
    event = SerializeEvent(UnifiedFreezeEventStream().publish_committed(BuildFreezeCommitted(restored.freeze)))

    assert history["pipeline"] == current["pipeline"] == event["pipeline"]
    assert history["freeze_version"] == current["freeze_version"] == restored.freeze.version
    assert [item["stage"] for item in history["pipeline"]["stage_snapshots"]] == list(_HISTORICAL_STAGES)
    assert len(history["pipeline"]["stages"]) == 16
    assert history["input_versions"] == dict(restored.freeze.input_versions)
    assert all(item["duration_ms"] is None for item in history["pipeline"]["stages"])
    with SqliteSession(tmp_path / "freeze.sqlite3") as connection:
        row = connection.run("SELECT record_bytes,freeze_hash,status FROM freeze_record").require_one()
        assert bytes(row[0]) == encoded and bytes(row[1]).hex() == restored.payload_hash and row[2] == "committed"


@pytest.mark.parametrize(
    "damage",
    (
        "stage_order",
        "batch",
        "counts",
        "flag",
        "age",
        "health",
        "duplicate_reason",
        "missing_stage",
        "missing_diagnostic",
        "diagnostic_state",
        "unknown_field",
        "bool_count",
    ),
)
def test_historical_audit_validates_frozen_protocol_even_with_consistent_hashes(damage):
    raw = json.loads(_historical_bytes())
    audit = raw["freeze"]["pipeline"]
    first = audit["stage_snapshots"][0]
    if damage == "stage_order":
        first["stage_order"] = 2
    elif damage == "batch":
        first["output_batch_id"] = "unrelated-batch"
    elif damage == "counts":
        first["failed_count"] = 1
    elif damage == "flag":
        first["degraded"] = False
    elif damage == "age":
        first["source_health"]["age_seconds"] = -1.0
    elif damage == "health":
        first["source_health"]["healthy_source_count"] = 3
    elif damage == "duplicate_reason":
        first["reasons"] *= 2
    elif damage == "missing_stage":
        audit["stage_snapshots"].pop()
    elif damage == "missing_diagnostic":
        audit["stages"].pop()
    elif damage == "diagnostic_state":
        audit["stages"][0]["state"] = "invented_state"
    elif damage == "unknown_field":
        first["new_runtime_field"] = "not-part-of-the-frozen-protocol"
    elif damage == "bool_count":
        first["output_count"] = True
    with pytest.raises(ValueError):
        CommittedRecordFromBytes(_resign(raw))


def test_historical_audit_cannot_enter_production_pipeline_builders():
    historical = CommittedRecordFromBytes(_historical_bytes()).freeze.pipeline
    production_audit = pipeline()
    with pytest.raises(ValueError, match="active diagnostic types"):
        production_pipeline.RecommendationPipelineStatus(production_audit.current_stage, historical.stages)
    with pytest.raises(ValueError, match="active stage snapshots"):
        replace(production_audit, stage_snapshots=historical.stage_snapshots)
    with pytest.raises(ValueError, match="active snapshot"):
        PipelineStageOutput((object(),), historical.stage_snapshots[0])
    published_snapshot = stage_snapshots()[0]
    for field in ("state", "source_health", "reasons"):
        with pytest.raises(ValueError, match="active .* types"):
            replace(published_snapshot, **{field: getattr(historical.stage_snapshots[0], field)})
    for field in ("metric_ranges", "facets", "reason_counts"):
        with pytest.raises(ValueError, match="active evidence types"):
            replace(production_audit.stages[0], **{field: getattr(historical.stages[0], field)})
    with pytest.raises(ValueError, match="active state type"):
        replace(published_snapshot.source_health, state=historical.stage_snapshots[0].source_health.state)
    with pytest.raises(ValueError, match="active severity type"):
        production_pipeline.StageReasonAggregate(
            "source_partial", "来源不足", 1, historical.stage_snapshots[0].reasons[0].severity
        )
    with pytest.raises(ValueError, match="active stage type"):
        StageOutput(
            historical.stage_snapshots[0].stage,
            (),
            input_batch_id="input",
            as_of=NOW,
            input_count=0,
            source_health=production_pipeline.SourceHealth(production_pipeline.SourceHealthState.READY, 1, 1),
            latency_ms=0,
        )


def test_recovery_idempotency_uses_verified_identity_across_production_and_historical_audits():
    original = CommittedFreezeRecord(
        FormalScoredFreeze(replace(freeze(), pipeline=replace(pipeline(), stage_snapshots=stage_snapshots()))),
        NOW.replace(hour=15, minute=0),
        "scheduled",
    )
    restored = CommittedRecordFromBytes(CommittedRecordBytes(original))
    index = UnifiedFreezeIndex()
    assert index.restore_formal(original)
    assert index.restore_formal(restored)
    assert index.commit_formal(original)
    conflicting = replace(original, freeze=replace(original.freeze, degraded_reasons=("different_content",)))
    assert conflicting.version == restored.version and conflicting.payload_hash != restored.payload_hash
    assert not index.restore_formal(conflicting)
    assert not index.commit_formal(conflicting)
    assert CommittedRecordBytes(index.snapshot(Strategy.TOMORROW).formal) == CommittedRecordBytes(original)


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_production_formal_publication_still_persists_only_six_rows(tmp_path, strategy):
    current = freeze(strategy)
    current = replace(
        current,
        pipeline=replace(pipeline(), stage_snapshots=stage_snapshots()),
        items=tuple(
            replace(
                current.items[0],
                code=f"60000{rank}",
                selected=rank <= 6,
                rank=rank if rank <= 6 else 0,
                selection_rank=rank,
                local_score=100.0 - rank,
                final_score=100.0 - rank,
                quote=None,
            )
            for rank in range(1, 9)
        ),
    )
    formal = CommittedFreezeRecord(FormalScoredFreeze(current), NOW.replace(hour=15, minute=0), "scheduled")
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", sqlite_policy=SqlitePolicyFixture())
    records.initialize()
    records.commit(formal)
    loaded = records.load(strategy, NOW.date())
    assert loaded is not None and isinstance(loaded.freeze.pipeline, HistoricalPipelineAudit)
    assert tuple(item.code for item in loaded.freeze.items) == tuple(f"60000{rank}" for rank in range(1, 7))
    assert loaded.freeze.items == formal.freeze.items
    assert CommittedRecordBytes(loaded) == CommittedRecordBytes(formal)
    with SqliteSession(tmp_path / "freeze.sqlite3") as connection:
        payload = connection.run("SELECT record_bytes FROM freeze_record").require_one()[0]
        assert len(json.loads(payload)["freeze"]["items"]) == 6


def test_historical_payload_limit_is_still_eight_mib(tmp_path):
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", sqlite_policy=SqlitePolicyFixture())
    records.initialize()
    with pytest.raises(FreezeRecordUnavailableError, match="bounded limit"):
        records.import_record_bytes(b" " * (8 * 1024 * 1024 + 1))
    assert records.list_dates(Strategy.TOMORROW) == ()

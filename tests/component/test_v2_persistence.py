from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trader.domain.models import (
    FeatureSnapshot,
    FusionMode,
    MarketQuote,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.infrastructure.persistence.snapshots import snapshot_bytes, snapshot_from_dict
from trader.infrastructure.persistence.sqlite import connect
from trader.infrastructure.persistence.writer import SnapshotConflictError, SnapshotRepository

NOW = datetime(2026, 7, 16, 6, 50, tzinfo=timezone.utc)


def test_snapshot_round_trip_preserves_frozen_input() -> None:
    snapshot = _snapshot()

    restored = snapshot_from_dict(json.loads(snapshot_bytes(snapshot)))

    assert restored == snapshot
    assert restored.recommendations[0].features.values["relative_strength_5d"] == 65.0


def test_publish_and_freeze_create_verified_manifest(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    snapshot = _snapshot()

    repository.publish(snapshot)
    repository.freeze(snapshot)

    latest = repository.latest(Strategy.TOMORROW)
    frozen = repository.load_frozen(Strategy.TOMORROW, "2026-07-16")
    assert latest == replace(snapshot, frozen=True)
    assert frozen is not None
    assert frozen.snapshot_id == snapshot.snapshot_id
    assert frozen.frozen is True
    assert repository.recommendation_dates(Strategy.TOMORROW) == ("2026-07-16",)
    with connect(tmp_path / "runtime.sqlite3") as connection:
        manifest = connection.execute("SELECT status, record_count FROM frozen_snapshots").fetchone()
        recommendation = connection.execute("SELECT stock_code, rank, anchor_price FROM recommendations").fetchone()
    assert tuple(manifest) == ("committed", 1)
    assert tuple(recommendation) == ("600001", 1, 12.0)


def test_recovery_commits_file_left_after_process_crash(tmp_path) -> None:
    def crash(stage: str) -> None:
        if stage == "frozen_file_created":
            raise SimulatedCrash

    repository = SnapshotRepository(tmp_path, config_version="runtime-v2", fault_injector=crash)
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())

    recovered = SnapshotRepository(tmp_path, config_version="runtime-v2")
    result = recovered.recover()

    assert result == {"recovered": 1, "quarantined": 0, "orphaned": 0}
    assert recovered.load_frozen(Strategy.TOMORROW, "2026-07-16") is not None


def test_committed_freeze_updates_latest_pointer_before_returning(tmp_path) -> None:
    def crash(stage: str) -> None:
        if stage == "manifest_committed":
            raise SimulatedCrash

    repository = SnapshotRepository(tmp_path, config_version="runtime-v2", fault_injector=crash)
    repository.initialize()

    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())

    latest = SnapshotRepository(tmp_path, config_version="runtime-v2").latest(Strategy.TOMORROW)
    assert latest is not None
    assert latest.snapshot_id == "snapshot-1"
    assert latest.frozen is True


def test_recovery_quarantines_hash_mismatch(tmp_path) -> None:
    def crash(stage: str) -> None:
        if stage == "frozen_file_created":
            raise SimulatedCrash

    repository = SnapshotRepository(tmp_path, config_version="runtime-v2", fault_injector=crash)
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())
    frozen_path = next((tmp_path / "frozen").rglob("*.json"))
    frozen_path.write_text("{}", encoding="utf-8")

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result["quarantined"] == 1
    with connect(tmp_path / "runtime.sqlite3") as connection:
        status = connection.execute("SELECT status FROM frozen_snapshots").fetchone()[0]
    assert status == "quarantined"


def test_freeze_is_idempotent_but_rejects_conflict(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    snapshot = _snapshot()
    repository.freeze(snapshot)
    repository.freeze(snapshot)

    with pytest.raises(SnapshotConflictError):
        repository.freeze(replace(snapshot, snapshot_id="different"))


def test_long_snapshot_cannot_be_frozen(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()

    with pytest.raises(ValueError, match="never frozen"):
        repository.freeze(replace(_snapshot(), strategy=Strategy.LONG))


class SimulatedCrash(RuntimeError):
    pass


def _snapshot() -> RecommendationSnapshot:
    quote = MarketQuote(
        code="600001",
        name="测试股份",
        price=12.0,
        previous_close=11.65,
        open_price=11.8,
        high=12.2,
        low=11.7,
        pct_change=3.0,
        change_5m=1.0,
        speed=0.8,
        volume_ratio=2.0,
        turnover_rate=3.0,
        amount=300_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry="工业",
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version="fixture-v1",
    )
    features = FeatureSnapshot(
        quote=quote,
        values={"relative_strength_5d": 65.0},
        observed_at=NOW,
        history_days=60,
    )
    score = ScoreBreakdown(
        components={"momentum": 82.0},
        base_score=82.0,
        local_risk_penalty=2.0,
        local_score=80.0,
        deepseek_score=100.0,
        confidence_coverage=1.0,
        deepseek_risk_penalty=3.0,
        final_score=83.4,
        fusion_mode=FusionMode.HYBRID,
        fusion_applied=True,
    )
    recommendation = Recommendation(
        strategy=Strategy.TOMORROW,
        features=features,
        score=score,
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=RecommendationAction.EXECUTABLE,
        action_reason="score_threshold_met",
        veto=False,
        rank=1,
    )
    return RecommendationSnapshot(
        snapshot_id="snapshot-1",
        strategy=Strategy.TOMORROW,
        trade_date="2026-07-16",
        phase="afternoon",
        data_version="fixture-v1",
        strategy_version="strategy-v6",
        fusion_version="fusion-v2",
        fusion_mode=FusionMode.HYBRID,
        published_at=NOW,
        recommendations=(recommendation,),
        filtered_count=2,
        filter_reasons={"stale_quote": 2},
    )

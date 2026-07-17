from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trader.domain.models import (
    CrossSectionStats,
    FeatureSnapshot,
    FusionMode,
    LiveOverlay,
    LiveQuote,
    MarketQuote,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.infrastructure.persistence.snapshots import snapshot_bytes, snapshot_from_dict, snapshot_sha256
from trader.infrastructure.persistence.sqlite import connect
from trader.infrastructure.persistence.writer import SnapshotConflictError, SnapshotRepository

NOW = datetime(2026, 7, 16, 6, 50, tzinfo=timezone.utc)


def test_snapshot_round_trip_preserves_frozen_input() -> None:
    snapshot = _snapshot()

    restored = snapshot_from_dict(json.loads(snapshot_bytes(snapshot)))

    assert restored == snapshot
    assert restored.recommendations[0].features.values["relative_strength_5d"] == 65.0
    normalization = restored.recommendations[0].features.normalization["relative_strength_5d"]
    assert (normalization.lower_bound, normalization.upper_bound) == (-8.0, 12.0)
    assert normalization.population_data_version == "fixture-v1"


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
        manifest = connection.execute(
            "SELECT status, record_count, schema_version, config_version, anchor_json FROM frozen_snapshots"
        ).fetchone()
        recommendation = connection.execute("SELECT stock_code, rank, anchor_price FROM recommendations").fetchone()
        published = connection.execute("SELECT snapshot_id, relative_path FROM published_snapshots").fetchone()
    assert tuple(manifest[:4]) == ("committed", 1, "recommendation_snapshot_v2", "runtime-v2")
    assert json.loads(manifest["anchor_json"])["600001"]["age_seconds"] == 0.0
    assert tuple(recommendation) == ("600001", 1, 12.0)
    assert tuple(published) == ("snapshot-1", "frozen/tomorrow/2026-07-16/snapshot-1.json")


def test_live_overlay_is_recoverable_without_changing_frozen_json(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    snapshot = _snapshot()
    repository.freeze(snapshot)
    frozen_path = next((tmp_path / "frozen").rglob("*.json"))
    original_digest = snapshot_sha256(frozen_path.read_bytes())
    quote = snapshot.recommendations[0].features.quote
    observed_at = NOW + timedelta(minutes=1)
    overlay = LiveOverlay(
        snapshot_id=snapshot.snapshot_id,
        strategy=snapshot.strategy,
        trade_date=snapshot.trade_date,
        version="overlay-v1",
        observed_at=observed_at,
        quotes={
            quote.code: LiveQuote(
                code=quote.code,
                price=12.3,
                pct_change=5.58,
                source="tencent",
                source_time=observed_at,
                received_time=observed_at,
                data_version="quote-v2",
            )
        },
    )

    repository.save_live_overlay(overlay)

    assert repository.load_live_overlay(snapshot.strategy, snapshot.trade_date) == overlay
    assert snapshot_sha256(frozen_path.read_bytes()) == original_digest
    closing = replace(
        overlay,
        version="overlay-close",
        observed_at=observed_at + timedelta(minutes=1),
        closing=True,
    )
    repository.save_live_overlay(closing)
    assert (
        repository.save_live_overlay(
            replace(overlay, version="overlay-late", observed_at=observed_at + timedelta(minutes=2))
        )
        is False
    )
    assert repository.load_live_overlay(snapshot.strategy, snapshot.trade_date) == closing


def test_new_trade_date_requires_a_new_snapshot_id(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    first = _snapshot()
    second = replace(first, snapshot_id="snapshot-2", trade_date="2026-07-17")

    repository.freeze(first)
    repository.freeze(second)

    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-16").snapshot_id == "snapshot-1"
    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-17").snapshot_id == "snapshot-2"


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


def test_recovery_quarantines_committed_hash_mismatch_and_restores_previous_freeze(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    first = _snapshot()
    second = replace(first, snapshot_id="snapshot-2", trade_date="2026-07-17")
    repository.freeze(first)
    repository.freeze(second)
    repository.publish(second)
    second_path = next(path for path in (tmp_path / "frozen").rglob("*.json") if "snapshot-2" in path.name)
    second_path.write_text("{}", encoding="utf-8")

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result["quarantined"] == 1
    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-17") is None
    assert repository.latest(Strategy.TOMORROW).snapshot_id == "snapshot-1"


def test_recovery_rejects_staged_manifest_version_mismatch(tmp_path) -> None:
    def crash(stage: str) -> None:
        if stage == "frozen_file_created":
            raise SimulatedCrash

    repository = SnapshotRepository(tmp_path, config_version="runtime-v2", fault_injector=crash)
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())
    with connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute("UPDATE frozen_snapshots SET config_version = 'different'")

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result["quarantined"] == 1
    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-16") is None


def test_freeze_rechecks_manifest_before_second_transaction(tmp_path) -> None:
    def tamper(stage: str) -> None:
        if stage == "frozen_file_created":
            with connect(tmp_path / "runtime.sqlite3") as connection:
                connection.execute("UPDATE frozen_snapshots SET strategy_version = 'tampered'")

    repository = SnapshotRepository(tmp_path, config_version="runtime-v2", fault_injector=tamper)
    repository.initialize()

    with pytest.raises(SnapshotConflictError, match="strategy_version_mismatch"):
        repository.freeze(_snapshot())


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
        normalization={"relative_strength_5d": CrossSectionStats(-8.0, 12.0, 360, 12, 0.025, 0.975, "fixture-v1")},
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
        config_version="runtime-v2",
    )

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from trader.application.ports.snapshots import RecoverySummary
from trader.domain.market.models import (
    Board,
    CrossSectionStats,
    Evidence,
    FeatureSnapshot,
    LiveQuote,
    MarketQuote,
)
from trader.domain.outcome.models import (
    BenchmarkReturn,
    RecommendationOutcome,
)
from trader.domain.recommendation.models import (
    FilterAudit,
    FusionMode,
    LiveOverlay,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.review.models import (
    DeepSeekReview,
    ReviewOutcome,
)
from trader.infra.persistence.recommendation_archive import list_bundles, verify_bundle
from trader.infra.persistence.snapshots import (
    snapshot_bytes,
    snapshot_from_dict,
    snapshot_sha256,
    snapshot_to_dict,
)
from trader.infra.persistence.sqlite import connect
from trader.infra.persistence.writer import SnapshotConflictError, SnapshotRepository

NOW = datetime(2026, 7, 16, 6, 50, tzinfo=timezone.utc)


def test_snapshot_round_trip_preserves_frozen_input() -> None:
    snapshot = _snapshot()

    payload = json.loads(snapshot_bytes(snapshot))
    restored = snapshot_from_dict(payload)

    assert restored == snapshot
    assert restored.recommendations[0].features.values["relative_strength_5d"] == 65.0
    assert restored.filter_details == snapshot.filter_details
    normalization = restored.recommendations[0].features.normalization["relative_strength_5d"]
    assert (normalization.lower_bound, normalization.upper_bound) == (-8.0, 12.0)
    assert normalization.population_data_version == "fixture-v1"
    tail_evidence = restored.recommendations[0].features.evidence[0]
    assert tail_evidence.received_at == NOW
    assert tail_evidence.data_version == "intraday-v1"
    assert restored.recommendations[0].features.values["tail_return_30m_pct"] == 2.0
    assert restored.recommendations[0].features.values["tail_volume_ratio_raw"] == 1.5
    payload.pop("filter_details")
    assert snapshot_from_dict(payload).filter_details == ()


def test_snapshot_round_trip_preserves_v15_board_and_merge_metadata() -> None:
    snapshot = _snapshot()
    recommendation = snapshot.recommendations[0]
    quote = replace(
        recommendation.features.quote,
        board=Board.MAIN,
        board_source="tushare",
        board_reliability="verified",
        exchange="SSE",
        listing_date=date(2020, 1, 2),
        listing_age_sessions=1000,
        has_price_limit=True,
        exchange_limit_pct=10.0,
        strategy_hot_cap_pct=8.0,
        rule_version="cn-board-rules-v1",
        rule_effective_date=date(2023, 8, 28),
    )
    updated = replace(
        snapshot,
        recommendations=(replace(recommendation, features=replace(recommendation.features, quote=quote)),),
        metadata={
            **snapshot.metadata,
            "merge_epoch": "merge-v15",
            "source_versions": {"eastmoney": "east-v1", "tushare": "master-v1"},
            "field_sources": {"600001": {"price": "eastmoney", "board": "tushare"}},
            "market_conflicts": [],
            "market_missing_reasons": {},
        },
    )

    restored = snapshot_from_dict(json.loads(snapshot_bytes(updated)))

    restored_quote = restored.recommendations[0].features.quote
    assert restored_quote.board is Board.MAIN
    assert restored_quote.board_source == "tushare"
    assert restored_quote.listing_date == date(2020, 1, 2)
    assert restored_quote.strategy_hot_cap_pct == 8.0
    assert restored.metadata["merge_epoch"] == "merge-v15"


def test_snapshot_round_trip_preserves_deepseek_review_audit_fields() -> None:
    base = _snapshot()
    reviewed = replace(
        base.recommendations[0],
        review=DeepSeekReview(
            code=base.recommendations[0].features.quote.code,
            outcome=ReviewOutcome.APPLIED,
            dimensions={},
            risk_facts=(),
            completed_at=NOW,
            rating="bearish",
            review_stage="primary",
            challenger_status="challenged",
            requested_model="deepseek-v4-flash",
            actual_model="deepseek-v4-pro",
            thinking_mode="standard",
            raw_confidence=0.91,
            calibrated_confidence=0.87,
            evidence_manifest_hash="sha-abc",
            calibration_version="v1",
        ),
    )
    snapshot = replace(base, recommendations=(reviewed, *base.recommendations[1:]))

    payload = snapshot_to_dict(snapshot)
    restored = snapshot_from_dict(payload)

    restored_review = restored.recommendations[0].review
    assert restored_review is not None
    assert restored_review.review_stage == "primary"
    assert restored_review.challenger_status == "challenged"
    assert restored_review.requested_model == "deepseek-v4-flash"
    assert restored_review.actual_model == "deepseek-v4-pro"
    assert restored_review.thinking_mode == "standard"
    assert restored_review.raw_confidence == 0.91
    assert restored_review.calibrated_confidence == 0.87
    assert restored_review.evidence_manifest_hash == "sha-abc"
    assert restored_review.calibration_version == "v1"
    assert restored_review.rating == "bearish"


def test_snapshot_from_dict_uses_default_review_audit_values_when_fields_missing() -> None:
    base = _snapshot()
    reviewed = replace(
        base.recommendations[0],
        review=DeepSeekReview(
            code=base.recommendations[0].features.quote.code,
            outcome=ReviewOutcome.APPLIED,
            dimensions={},
            risk_facts=(),
            completed_at=NOW,
            rating="neutral",
            review_stage="secondary",
            challenger_status="passed",
            requested_model="deepseek-v4-flash",
            actual_model="deepseek-v4-flash",
            thinking_mode="reasoning",
            raw_confidence=0.45,
            calibrated_confidence=0.33,
            evidence_manifest_hash="sha-default",
            calibration_version="v2",
        ),
    )
    snapshot = replace(base, recommendations=(reviewed, *base.recommendations[1:]))
    payload = snapshot_to_dict(snapshot)

    review_payload = payload["recommendations"][0]["review"]
    assert isinstance(review_payload, dict)
    for key in (
        "review_stage",
        "challenger_status",
        "requested_model",
        "actual_model",
        "thinking_mode",
        "raw_confidence",
        "calibrated_confidence",
        "evidence_manifest_hash",
        "calibration_version",
    ):
        review_payload.pop(key, None)

    restored = snapshot_from_dict(payload)
    restored_review = restored.recommendations[0].review
    assert restored_review is not None
    assert restored_review.review_stage == "primary"
    assert restored_review.challenger_status == "not_run"
    assert restored_review.requested_model is None
    assert restored_review.actual_model is None
    assert restored_review.thinking_mode is None
    assert restored_review.raw_confidence is None
    assert restored_review.calibrated_confidence is None
    assert restored_review.evidence_manifest_hash is None
    assert restored_review.calibration_version is None
    assert restored_review.rating == "neutral"


def test_freeze_creates_verified_manifest_without_published_draft_pointer(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    snapshot = _snapshot()

    repository.freeze(snapshot)

    frozen = repository.load_frozen(Strategy.TOMORROW, "2026-07-16")
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
    assert published is None


def test_checkpoint_is_hash_verified_and_consumed_once(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    boundary = NOW.replace(hour=14, minute=50, second=0, microsecond=0)
    snapshot = replace(_snapshot(), published_at=boundary - timedelta(seconds=10))

    repository.save_checkpoint(snapshot, boundary_at=boundary)

    assert repository.load_checkpoint(Strategy.TOMORROW, snapshot.trade_date, boundary_at=boundary) == snapshot
    repository.consume_checkpoint(Strategy.TOMORROW, snapshot.trade_date, boundary_at=boundary)
    assert repository.load_checkpoint(Strategy.TOMORROW, snapshot.trade_date, boundary_at=boundary) is None


def test_outcome_audit_is_idempotent_without_changing_frozen_snapshot(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    snapshot = _snapshot()
    repository.freeze(snapshot)
    frozen_path = next((tmp_path / "frozen").rglob("*.json"))
    frozen_digest = snapshot_sha256(frozen_path.read_bytes())

    targets = repository.pending_outcome_targets(limit=10)
    assert len(targets) == 1
    assert targets[0].atr20_pct == 2.0
    repository.record_benchmark_return(BenchmarkReturn("2026-07-17", 0.5), observed_at=NOW)
    assert repository.benchmark_returns_after("2026-07-16", limit=1) == (BenchmarkReturn("2026-07-17", 0.5),)
    incomplete = RecommendationOutcome(
        snapshot_id="snapshot-1",
        strategy=Strategy.TOMORROW,
        recommend_date="2026-07-16",
        stock_code="600001",
        horizon=1,
        status="insufficient_data",
        settled_at=NOW,
        anchor_price=12.0,
        atr20_pct=2.0,
        quality_reason="source_unavailable",
    )
    repository.save_recommendation_outcomes((incomplete,))
    assert repository.pending_outcome_targets(limit=10) == targets

    repository.save_recommendation_outcomes((replace(incomplete, status="complete", quality_reason=""),))

    assert repository.pending_outcome_targets(limit=10) == ()
    assert snapshot_sha256(frozen_path.read_bytes()) == frozen_digest


def test_recommendation_store_keeps_twenty_dates_and_archives_older_outcome_backlog(tmp_path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    start = date(2026, 6, 1)
    for offset in range(21):
        trade_date = (start + timedelta(days=offset)).isoformat()
        repository.freeze(
            replace(
                _snapshot(),
                snapshot_id=f"snapshot-{trade_date}",
                trade_date=trade_date,
            )
        )

    active_dates = repository.recommendation_dates(Strategy.TOMORROW)
    bundles = list_bundles(tmp_path)

    assert len(active_dates) == 20
    assert active_dates[-1] == "2026-06-02"
    assert len(bundles) == 1
    assert bundles[0]["trade_date"] == "2026-06-01"
    bundle_path = tmp_path / bundles[0]["relative_path"]
    assert verify_bundle(bundle_path)["schema"] == "recommendations-v1"
    assert repository.load_frozen(Strategy.TOMORROW, "2026-06-01") is None

    targets = repository.pending_outcome_targets(limit=100)
    archived_target = next(target for target in targets if target.recommend_date == "2026-06-01")
    repository.save_recommendation_outcomes(
        (
            RecommendationOutcome(
                snapshot_id=archived_target.snapshot_id,
                strategy=archived_target.strategy,
                recommend_date=archived_target.recommend_date,
                stock_code=archived_target.stock_code,
                horizon=1,
                status="complete",
                settled_at=NOW,
                anchor_price=archived_target.anchor_price,
                atr20_pct=archived_target.atr20_pct,
            ),
        )
    )

    assert all(target.recommend_date != "2026-06-01" for target in repository.pending_outcome_targets(limit=100))
    assert verify_bundle(bundle_path)["schema"] == "recommendations-v1"


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

    assert result == RecoverySummary(recovered=1, quarantined=0, orphaned=0)
    assert recovered.load_frozen(Strategy.TOMORROW, "2026-07-16") is not None


def test_committed_freeze_is_immediately_readable_without_draft_pointer(tmp_path) -> None:
    def crash(stage: str) -> None:
        if stage == "manifest_committed":
            raise SimulatedCrash

    repository = SnapshotRepository(tmp_path, config_version="runtime-v2", fault_injector=crash)
    repository.initialize()

    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())

    latest = SnapshotRepository(tmp_path, config_version="runtime-v2").load_frozen(Strategy.TOMORROW, "2026-07-16")
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

    assert result.quarantined == 1
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
    second_path = next(path for path in (tmp_path / "frozen").rglob("*.json") if "snapshot-2" in path.name)
    second_path.write_text("{}", encoding="utf-8")

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result.quarantined == 1
    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-17") is None
    assert repository.load_frozen(Strategy.TOMORROW, "2026-07-16").snapshot_id == "snapshot-1"


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

    assert result.quarantined == 1
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
        values={
            "relative_strength_5d": 65.0,
            "tail_return_30m_pct": 2.0,
            "tail_return_30m": 100.0,
            "tail_volume_ratio_raw": 1.5,
            "tail_volume_ratio": 75.0,
            "atr20_pct": 2.0,
        },
        observed_at=NOW,
        history_days=60,
        evidence=(Evidence("tail-1", "intraday_tail", "tail input", "eastmoney_intraday", NOW, NOW, "intraday-v1"),),
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
        filter_details=(FilterAudit("600001", "stale_quote", "<= 20s", 21.0, "fixture", NOW),),
        config_version="runtime-v2",
    )

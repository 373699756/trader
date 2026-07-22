from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.application.ports.youhua import (
    CONTRACT_VERSION,
    DEEPSEEK_V4_FACTS_VERSION,
    EVIDENCE_MANIFEST_VERSION,
    LOGICAL_CACHE_LIMIT_BYTES,
    MARKET_CHANGE_SET_VERSION,
    P3_P4_SCHEMA_VERSION,
    P4_P5_SCHEMA_VERSION,
    P6_OVERLAY_EVENT_VERSION,
    P6_PROJECTION_EVENT_VERSION,
    PROCESS_PEAK_RSS_LIMIT_BYTES,
    REVIEW_OWNER_IDENTITY_VERSION,
    DeepSeekV4Facts,
    DirectionalFact,
    EvidenceManifest,
    EvidenceManifestItem,
    FeatureSnapshotEnvelope,
    HighValueReviewInput,
    HighValueReviewManifest,
    MarketChangeSet,
    MemoryUsageSnapshot,
    OverlayEvent,
    OverlayQuote,
    PriceReactionFact,
    ProjectionEvent,
    ProjectionUpsert,
    ResyncReason,
    ReviewOwnerIdentity,
    RiskFactFlags,
    YouhuaContractError,
    default_memory_budget_contract,
    public_schema_versions,
    validate_memory_activation,
)
from trader.application.youhua_test_doubles import P4ConsumerStub, ProjectionOverlayProducerStub, ReviewInputStub
from trader.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import FusionMode, Strategy
from trader.infra.settings import load_runtime_settings
from trader.infra.settings_parser import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"
NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def test_a2_public_schema_versions_and_resync_reasons_are_single_owner() -> None:
    versions = public_schema_versions()

    assert versions["contract"] == CONTRACT_VERSION
    assert versions["p3_p4"] == P3_P4_SCHEMA_VERSION
    assert versions["market_change_set"] == MARKET_CHANGE_SET_VERSION
    assert versions["p4_p5"] == P4_P5_SCHEMA_VERSION
    assert versions["deepseek_facts"] == DEEPSEEK_V4_FACTS_VERSION
    assert versions["evidence_manifest"] == EVIDENCE_MANIFEST_VERSION
    assert versions["review_owner_identity"] == REVIEW_OWNER_IDENTITY_VERSION
    assert versions["p6_projection"] == P6_PROJECTION_EVENT_VERSION
    assert versions["p6_overlay"] == P6_OVERLAY_EVENT_VERSION
    assert {reason.value for reason in ResyncReason} == {
        "cursor_expired",
        "cursor_ahead",
        "cursor_gap",
        "slow_subscriber",
        "base_mismatch",
        "schema_mismatch",
        "identity_mismatch",
    }


def test_a2_p3_p4_envelope_and_p4_consumer_stub_record_identity_only() -> None:
    change_set = MarketChangeSet(
        schema_version=MARKET_CHANGE_SET_VERSION,
        merge_epoch="merge-1",
        previous_merge_epoch=None,
        updated_codes=("600001",),
        dirty_codes=("600001",),
        dirty_field_families=("quote",),
        evidence_manifest_hash=HASH,
        content_hash=HASH,
    )
    envelope = FeatureSnapshotEnvelope(
        schema_version=P3_P4_SCHEMA_VERSION,
        snapshot_version="snapshot-v1",
        feature_snapshot_version="features-v1",
        trade_date="2026-07-22",
        phase="today_main",
        merge_epoch="merge-1",
        data_version="market-v1",
        config_version="config-v1",
        feature_schema_version="feature-v1",
        content_hash=HASH,
        feature_snapshots=(_feature("600001"),),
        market_change_set=change_set,
    )

    receipt = P4ConsumerStub().consume(envelope)

    assert receipt.merge_epoch == "merge-1"
    assert receipt.content_hash == HASH
    assert receipt.feature_count == 1
    assert receipt.changed_codes == ("600001",)
    assert receipt.dirty_field_families == ("quote",)


def test_a2_review_manifest_rejects_long_requests_and_invalid_identity() -> None:
    evidence = _evidence_manifest()
    owner = _review_owner(Strategy.TODAY)
    review_input = HighValueReviewInput(
        contract_version=P4_P5_SCHEMA_VERSION,
        strategy=Strategy.TODAY,
        trade_date="2026-07-22",
        phase="today_main",
        deadline=NOW,
        owner_identity=owner,
        candidate_code="600001",
        feature_snapshot_identity=HASH,
        local_score=81.5,
        local_rank=1,
        action_threshold=80.0,
        in_protection_set=False,
        near_action_threshold=True,
        near_global_boundary=False,
        topk_boundary=True,
        has_new_high_risk=False,
        has_new_catalyst=True,
        direction_conflict=False,
        evidence_conflict=False,
        was_reviewed=False,
        evidence_manifest_hash=evidence.manifest_hash,
        price_reaction_bucket="not_reacted",
        budget_bucket="today",
    )
    manifest = HighValueReviewManifest(
        schema_version=P4_P5_SCHEMA_VERSION,
        strategy=Strategy.TODAY,
        trade_date="2026-07-22",
        phase="today_main",
        evidence_manifest=evidence,
        inputs=(review_input,),
    )

    assert ReviewInputStub().collect(manifest) == ("600001",)
    with pytest.raises(YouhuaContractError, match="long review input collection"):
        HighValueReviewManifest(
            schema_version=P4_P5_SCHEMA_VERSION,
            strategy=Strategy.LONG,
            trade_date="2026-07-22",
            phase="long",
            evidence_manifest=evidence,
            inputs=(review_input,),
        )
    with pytest.raises(YouhuaContractError, match="feature_snapshot_identity"):
        HighValueReviewInput(
            **{
                **review_input.__dict__,
                "feature_snapshot_identity": "",
            }
        )


def test_a2_deepseek_v4_facts_are_evidence_bounded_and_score_free() -> None:
    manifest = _evidence_manifest()
    facts = DeepSeekV4Facts(
        contract_version=DEEPSEEK_V4_FACTS_VERSION,
        code="600001",
        abstain=False,
        catalyst=DirectionalFact(
            direction="positive",
            importance="high",
            confirmation="official",
            cycle="short",
            evidence_ids=("evidence-1",),
        ),
        price_reaction=PriceReactionFact(bucket="not_reacted", evidence_ids=("evidence-1",)),
        fundamental=DirectionalFact(direction="unknown"),
        industry_policy=DirectionalFact(direction="neutral"),
        risks=RiskFactFlags(regulatory=False),
        conflicts=(),
        coverage=("catalyst",),
    )

    facts.validate_against_manifest(manifest)
    with pytest.raises(TypeError):
        DeepSeekV4Facts(
            **{
                **facts.__dict__,
                "penalty": 10,
            }
        )
    with pytest.raises(YouhuaContractError, match="outside the manifest"):
        DeepSeekV4Facts(
            **{
                **facts.__dict__,
                "catalyst": DirectionalFact(direction="positive", evidence_ids=("missing",)),
            }
        ).validate_against_manifest(manifest)


def test_a2_projection_overlay_stub_preserves_cas_and_overlay_boundaries() -> None:
    projection = ProjectionEvent(
        schema_version=P6_PROJECTION_EVENT_VERSION,
        event_id="event-1",
        projection_version="projection-2",
        base_projection_version="projection-1",
        etag="etag-1",
        snapshot_id="snapshot-1",
        strategy=Strategy.TODAY,
        trade_date="2026-07-22",
        view="official",
        phase="today_main",
        published_at=NOW,
        strategy_version="strategy-v1",
        fusion_mode=FusionMode.HYBRID,
        stale=False,
        frozen=False,
        degraded_reasons=(),
        filtered_count=0,
        upserts=(ProjectionUpsert(code="600001", rank=1, action="observe", score=83.4),),
        removed_codes=(),
    )
    overlay = OverlayEvent(
        schema_version=P6_OVERLAY_EVENT_VERSION,
        event_id="overlay-1",
        projection_version="projection-2",
        overlay_version="overlay-1",
        snapshot_id="snapshot-1",
        strategy=Strategy.TODAY,
        trade_date="2026-07-22",
        observed_at=NOW,
        closing=False,
        quotes=(
            OverlayQuote(
                code="600001",
                price=12.3,
                pct_change=1.2,
                source="fixture",
                source_time=NOW,
                quote_data_version="quote-v1",
            ),
        ),
    )
    producer = ProjectionOverlayProducerStub()

    assert producer.publish_projection(projection) == "projection-2"
    assert producer.publish_overlay(overlay) == "overlay-1"
    assert producer.projections == (projection,)
    assert producer.overlays == (overlay,)
    with pytest.raises(YouhuaContractError, match="upsert and remove"):
        ProjectionEvent(
            **{
                **projection.__dict__,
                "removed_codes": ("600001",),
            }
        )


def test_a2_memory_contract_distinguishes_logical_cache_and_process_peak(tmp_path) -> None:
    runtime = load_runtime_settings(RUNTIME_CONFIG)

    assert runtime.performance_budgets.memory.cache_logical_bytes == LOGICAL_CACHE_LIMIT_BYTES
    assert runtime.performance_budgets.memory.process_peak_rss_bytes == PROCESS_PEAK_RSS_LIMIT_BYTES
    assert runtime.market_data.cache_policy.total_bytes == LOGICAL_CACHE_LIMIT_BYTES
    assert runtime.market_data.cache_policy.pool_total_bytes != PROCESS_PEAK_RSS_LIMIT_BYTES
    assert default_memory_budget_contract().to_status()["process_peak_rss_bytes"] == PROCESS_PEAK_RSS_LIMIT_BYTES
    assert not validate_memory_activation(
        MemoryUsageSnapshot(LOGICAL_CACHE_LIMIT_BYTES + 1, PROCESS_PEAK_RSS_LIMIT_BYTES)
    ).allowed
    assert not validate_memory_activation(
        MemoryUsageSnapshot(LOGICAL_CACHE_LIMIT_BYTES, PROCESS_PEAK_RSS_LIMIT_BYTES + 1)
    ).allowed

    raw = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    raw["performance_budgets"]["memory"] = {"cache_total_bytes": 268435456, "growth_percent": 20}
    changed_path = tmp_path / "runtime.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown keys|cache_logical_bytes"):
        load_runtime_settings(changed_path)


def _feature(code: str) -> FeatureSnapshot:
    return FeatureSnapshot(
        quote=MarketQuote(
            code=code,
            name="测试",
            price=12.3,
            previous_close=12.0,
            open_price=12.1,
            high=12.5,
            low=12.0,
            pct_change=1.2,
            change_5m=0.2,
            speed=0.1,
            volume_ratio=1.1,
            turnover_rate=2.0,
            amount=10_000_000.0,
            amplitude=3.0,
            market_cap=5_000_000_000.0,
            industry="测试",
            source="fixture",
            source_time=NOW,
            received_time=NOW,
            data_version="quote-v1",
            board=Board.MAIN,
        ),
        values={"momentum": 80.0},
        observed_at=NOW,
        merge_epoch="merge-1",
    )


def _evidence_manifest() -> EvidenceManifest:
    return EvidenceManifest(
        schema_version=EVIDENCE_MANIFEST_VERSION,
        manifest_hash=HASH,
        items=(
            EvidenceManifestItem(
                evidence_id="evidence-1",
                evidence_type="announcement",
                source_tier="official",
                source="exchange",
                published_at=NOW,
                received_at=NOW,
                data_version="evidence-v1",
                event_key="event-key-1",
                supports_positive_fact=True,
                counter_evidence=False,
                price_reaction_bucket="not_reacted",
            ),
        ),
    )


def _review_owner(strategy: Strategy) -> ReviewOwnerIdentity:
    return ReviewOwnerIdentity(
        schema_version=REVIEW_OWNER_IDENTITY_VERSION,
        owner_strategy=strategy,
        consumer_strategy=strategy,
        generation="generation-1",
        budget_bucket=strategy.value,
        model_role="primary",
        model="deepseek-v4-flash",
        thinking_mode="standard",
        prompt_version="prompt-v1",
        facts_schema_version=DEEPSEEK_V4_FACTS_VERSION,
        config_version="config-v1",
    )

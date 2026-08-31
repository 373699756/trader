from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.recommendation.models import Strategy
from trader.domain.research.tomorrow_profile_comparison import (
    TOMORROW_PROFILE_COMPARISON_SPEC,
    TomorrowProfilePair,
    TomorrowProfilePairManifest,
    TomorrowProfilePrediction,
)
from trader.infra.persistence.tomorrow_profile_comparison import (
    SQLiteTomorrowProfileEvidenceStore,
    TomorrowProfileEvidenceConflictError,
)

NOW = datetime(2026, 8, 31, 14, 49, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _prediction(profile_id: str, *, selected: bool = False) -> TomorrowProfilePrediction:
    return TomorrowProfilePrediction(
        profile_id=profile_id,  # type: ignore[arg-type]
        model_version=f"model-{profile_id}:{profile_id * 64}",
        predicted_excess_return_pct=0.8,
        estimated_cost_pct=0.2,
        predicted_net_excess_pct=0.6,
        signal_score=70.0,
        local_score=70.0,
        model_disagreement_pct=0.0 if profile_id == "v1" else 0.1,
        action="unavailable",
        selected=selected,
        rank=1 if selected else 0,
    )


def _manifest() -> TomorrowProfilePairManifest:
    pair = TomorrowProfilePair(
        input_version="native-input:fixture",
        trade_date=NOW.date(),
        code="600001",
        board="main",
        industry="工业",
        anchor_price=10.0,
        atr20_pct=2.0,
        v1=_prediction("v1"),
        v2=_prediction("v2"),
    )
    return TomorrowProfilePairManifest(
        spec_hash=TOMORROW_PROFILE_COMPARISON_SPEC.content_hash,
        input_version=pair.input_version,
        trade_date=NOW.date(),
        observed_at=NOW,
        active_profile_id="v1",
        v1_model_version=pair.v1.model_version,
        v2_model_version=pair.v2.model_version,
        common_candidate_count=1,
        v1_scorable_count=1,
        v2_scorable_count=1,
        pairs=(pair,),
    )


def _outcome() -> RecommendationOutcome:
    return RecommendationOutcome(
        snapshot_id="native-input:fixture",
        strategy=Strategy.TOMORROW,
        recommend_date=NOW.date().isoformat(),
        stock_code="600001",
        horizon=1,
        status="complete",
        settled_at=NOW + timedelta(days=1),
        anchor_price=10.0,
        atr20_pct=2.0,
        minimum_low=9.9,
        end_close=10.2,
        gross_return_pct=2.0,
        benchmark_return_pct=0.5,
        net_excess_return_pct=1.3,
        mae_pct=-1.0,
        mae_atr=-0.5,
        severe_drawdown=False,
    )


def test_store_keeps_zero_selection_pairs_and_binds_only_the_exact_formal_input(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    manifest = _manifest()
    store.save_manifest(manifest)
    store.save_manifest(manifest)
    store.bind_formal_input(
        trade_date=NOW.date(),
        input_version=manifest.input_version,
        record_version="record:tomorrow:fixture",
        committed_at=NOW + timedelta(seconds=30),
    )

    targets = store.pending_formal_targets(limit=10)

    assert len(targets) == 1
    assert not targets[0].pair.v1.selected
    assert not targets[0].pair.v2.selected
    assert targets[0].input_version == manifest.input_version
    assert store.status().formal_manifests == 1
    with pytest.raises(ValueError, match="no matching paired manifest"):
        store.bind_formal_input(
            trade_date=date(2026, 9, 1),
            input_version="native-input:missing",
            record_version="record:missing",
            committed_at=NOW + timedelta(days=1),
        )


def test_store_rejects_manifest_conflicts_and_round_trips_complete_outcomes(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    manifest = _manifest()
    store.save_manifest(manifest)
    with pytest.raises(TomorrowProfileEvidenceConflictError, match="prediction manifest"):
        store.save_manifest(replace(manifest, active_profile_id="v2"))
    store.bind_formal_input(
        trade_date=NOW.date(),
        input_version=manifest.input_version,
        record_version="record:tomorrow:fixture",
        committed_at=NOW + timedelta(seconds=30),
    )
    store.save_outcomes((_outcome(),))

    assert store.complete_outcomes() == (_outcome(),)
    status = store.status()
    assert status.settled_pairs == 1
    assert status.complete_pairs == 1
    assert status.independent_days == 0
    assert status.state == "collecting"


def test_missing_store_status_is_read_only(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)

    status = store.status()
    inspected = store.inspect_status()
    report = store.inspect_terminal_report_bytes()

    assert status.initialized is False
    assert inspected == status
    assert report is None
    assert status.required_independent_days == 522
    assert not (tmp_path / "research").exists()


def test_formal_binding_prunes_only_same_day_nonformal_manifests(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    formal = _manifest()
    provisional = replace(
        formal,
        input_version="native-input:provisional",
        pairs=(replace(formal.pairs[0], input_version="native-input:provisional"),),
    )
    store.save_manifest(provisional)
    store.save_manifest(formal)

    store.bind_formal_input(
        trade_date=NOW.date(),
        input_version=formal.input_version,
        record_version="record:tomorrow:fixture",
        committed_at=NOW + timedelta(seconds=30),
    )

    assert store.load_manifest(formal.input_version) == formal
    assert store.load_manifest(provisional.input_version) is None
    assert store.status().prediction_manifests == 1


def test_zero_atr_pair_is_retained_for_explicit_insufficient_settlement(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    manifest = _manifest()
    pair = replace(manifest.pairs[0], atr20_pct=None)

    store.save_manifest(replace(manifest, pairs=(pair,)))

    loaded = store.load_manifest(manifest.input_version)
    assert loaded is not None
    assert loaded.pairs[0].atr20_pct is None


def test_store_rejects_outcomes_outside_the_bound_formal_manifest(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    manifest = _manifest()
    store.save_manifest(manifest)

    with pytest.raises(ValueError, match="formally bound"):
        store.save_outcomes((_outcome(),))

    store.bind_formal_input(
        trade_date=NOW.date(),
        input_version=manifest.input_version,
        record_version="record:tomorrow:fixture",
        committed_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(ValueError, match="stock code"):
        store.save_outcomes((replace(_outcome(), stock_code="600002"),))

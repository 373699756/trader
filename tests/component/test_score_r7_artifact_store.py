from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from trader.application.research.replay_models import canonical_hash
from trader.application.research.score_r7_models import (
    ScoreR7GateResult,
    ScoreR7ParameterProposal,
    ScoreR7PromotionDossier,
    ScoreR7SampleCounts,
    ScoreR7SensitivityResult,
)
from trader.infra.research.score_r7_artifacts import ScoreR7ArtifactConflictError, ScoreR7ArtifactStore


def _dossier() -> ScoreR7PromotionDossier:
    sensitivity = tuple(
        ScoreR7SensitivityResult(
            cost_bps=cost,
            block_days=block,
            sample_days=20,
            local_mean_gain_pct=0.2,
            local_confidence_lower_pct=0.1,
            local_confidence_upper_pct=0.3,
            local_p_value=0.01,
            local_bootstrap_seed=7,
            hybrid_mean_increment_pct=0.3,
            hybrid_confidence_lower_pct=0.1,
            hybrid_confidence_upper_pct=0.4,
            hybrid_p_value=0.01,
            hybrid_bootstrap_seed=8,
        )
        for cost in (20, 50, 100)
        for block in (3, 5, 10)
    )
    return ScoreR7PromotionDossier(
        dossier_identity="score_r6_forward_20261201_v1_promotion_dossier_v1",
        source_research_identity="score_r6_forward_20261201_v1",
        historical_report_hash="1" * 64,
        forward_spec_hash="2" * 64,
        forward_report_hash="3" * 64,
        day_manifest_hashes=tuple(str(index).zfill(64) for index in range(20)),
        trading_calendar_hash="4" * 64,
        rule_identity_hash="5" * 64,
        config_strategy_identity_hash="6" * 64,
        data_schema_version="score_r6_forward_same_stock_v1",
        strategy_version="strategy_review30_top6_observe6_2026_07",
        fusion_version="fusion_local68_deepseek32",
        engine_version="score_r6_forward_gate_v1",
        statistical_program_version="score_r7_sensitivity_mbb_v1",
        production_scope="local_only",
        proposed_parameters=ScoreR7ParameterProposal(
            candidate_hash="7" * 64,
            component_names=(
                "tail_structure",
                "turnover_flow",
                "trend",
                "stability",
                "market_state",
                "entry_quality",
            ),
            board_weight_units=(
                ("main", (1667, 556, 2222, 2777, 1111, 1667)),
                ("chinext", (1667, 556, 2222, 2777, 1111, 1667)),
                ("star", (1667, 556, 2222, 2777, 1111, 1667)),
            ),
            action_threshold=78,
            risk_penalty=4,
        ),
        sensitivity=sensitivity,
        gate_results=(
            ScoreR7GateResult("hybrid_confidence_lower_pct", 0.0, "greater_than", 0.0, False, False),
            ScoreR7GateResult("hybrid_mean_increment_pct", 0.0, "at_least", 0.2, False, False),
            ScoreR7GateResult("hybrid_p_value", 1.0, "at_most", 0.05, False, False),
            ScoreR7GateResult("local_maximum_board_fraction", 0.6, "at_most", 0.6, True, True),
            ScoreR7GateResult("local_maximum_stock_weight", 0.2, "at_most", 0.25, True, True),
            ScoreR7GateResult("local_mean_gain_pct", 0.2, "at_least", 0.2, True, True),
            ScoreR7GateResult("local_recall", 0.8, "at_least", 0.8, True, True),
            ScoreR7GateResult("local_severe_rate_delta", 0.02, "at_most", 0.02, True, True),
            ScoreR7GateResult("local_stability_delta", 0.25, "at_most", 0.25, True, True),
            ScoreR7GateResult("local_turnover_delta", 0.05, "at_most", 0.05, True, True),
        ),
        failed_trade_dates=(),
        sample_counts=ScoreR7SampleCounts(20, 20, 0, 300),
        ablation_ids=("hybrid_vs_local", "local_vs_production"),
        maximum_stock_weight=0.2,
        maximum_board_fraction=0.6,
        residual_risks=("manual_review_required", "production_release_not_authorized"),
    )


def test_r7_dossier_store_is_idempotent_and_tamper_evident(tmp_path) -> None:  # noqa: ANN001
    store = ScoreR7ArtifactStore(tmp_path)
    dossier = _dossier()

    assert store.seal(dossier) == dossier.content_hash
    assert store.seal(dossier) == dossier.content_hash
    assert store.inspect()["dossiers"][0]["manual_review_status"] == "pending"

    path = tmp_path / dossier.dossier_identity / "promotion-dossier.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["production_change_authorized"] = True
    unsigned = {key: value for key, value in payload.items() if key != "content_hash"}
    payload["content_hash"] = canonical_hash(unsigned)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScoreR7ArtifactConflictError, match="schema"):
        store.inspect()

    assert asdict(dossier)["production_change_authorized"] is False


def test_r7_dossier_rejects_path_like_research_identity() -> None:
    dossier = _dossier()
    payload = asdict(dossier)
    payload.pop("content_hash")
    payload["source_research_identity"] = "score_r6_forward_../../escape"
    payload["dossier_identity"] = "score_r6_forward_../../escape_promotion_dossier_v1"

    with pytest.raises(ValueError, match="source identity"):
        ScoreR7PromotionDossier(**payload)

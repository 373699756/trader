from __future__ import annotations

from dataclasses import replace

import pytest

from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.tomorrow_historical_p2 import (
    TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
    TOMORROW_HISTORICAL_P2_SPEC,
)
from trader.domain.research.tomorrow_shadow_preregistration import TOMORROW_SHADOW_P1_SPEC


def test_p2_spec_binds_h0_field_eligibility_single_candidate_and_all_gates() -> None:
    spec = TOMORROW_HISTORICAL_P2_SPEC
    statuses = {item.field_id: item.status for item in spec.field_eligibility}

    assert spec.research_identity == "score_tomorrow_historical_p2_v1"
    assert spec.source_research_identity == SCORE_H0_V1_SPEC.research_identity
    assert spec.source_spec_hash == SCORE_H0_V1_SPEC.content_hash
    assert spec.training_window == (SCORE_H0_V1_SPEC.training_start, SCORE_H0_V1_SPEC.training_end)
    assert spec.validation_window == (SCORE_H0_V1_SPEC.validation_start, SCORE_H0_V1_SPEC.validation_end)
    assert spec.candidate.candidate_id == TOMORROW_HISTORICAL_P2_CANDIDATE_ID
    assert spec.candidate.model_families == ("linear", "lightgbm")
    assert spec.candidate.model_weights == (0.5, 0.5)
    assert spec.candidate.model_random_seed == 20260830
    assert spec.candidate.lightgbm_num_threads == 1
    assert spec.selection_rule == "single_candidate_pass_or_stop_v1"
    assert spec.portfolio_sort_order == (
        "net_utility_desc",
        "severe_loss_probability_asc",
        "model_disagreement_asc",
        "code_asc",
    )
    assert spec.allow_empty_portfolio is True
    assert spec.comparator_id == "score_h0_ohlcv_cross_section_v1"
    assert spec.cost_rates == (0.002, 0.005, 0.01)
    assert spec.minimum_archive_coverage == 0.95
    assert spec.minimum_validation_pairs == 300
    assert spec.bootstrap_block_days == 5
    assert spec.bootstrap_repetitions == 10_000
    assert spec.production_authority is False
    assert spec.forward_research_identity is None
    assert spec.forward_trade_dates == ()
    assert statuses["qfq_return_1d"] == "eligible"
    assert statuses["amihud_20d"] == "eligible"
    assert statuses["historical_st_status"] == "not_reconstructed"
    assert statuses["historical_industry"] == "not_reconstructed"
    assert statuses["intraday_1450_tail"] == "not_reconstructed"
    assert statuses["deepseek_facts_point_in_time"] == "not_reconstructed"
    assert "score_tomorrow_shadow_p1_v1" in spec.excluded_evidence_identities
    assert TOMORROW_SHADOW_P1_SPEC.content_hash != spec.content_hash


def test_p2_spec_rejects_field_gate_candidate_or_forward_mutation() -> None:
    spec = TOMORROW_HISTORICAL_P2_SPEC

    with pytest.raises(ValueError, match="field eligibility matrix"):
        replace(spec, field_eligibility=spec.field_eligibility[:-1])
    with pytest.raises(ValueError, match="candidate family"):
        replace(spec, candidate=replace(spec.candidate, model_weights=(1.0, 0.0)))
    with pytest.raises(ValueError, match="historical gates"):
        replace(spec, minimum_validation_pairs=299)
    with pytest.raises(ValueError, match="candidate family"):
        replace(spec, candidate=replace(spec.candidate, model_random_seed=1))
    with pytest.raises(ValueError, match="historical gates"):
        replace(spec, portfolio_sort_order=("code_asc",))
    with pytest.raises(ValueError, match="cannot bind a forward identity"):
        replace(spec, forward_research_identity="score_tomorrow_shadow_p2_v1")
    with pytest.raises(ValueError, match="cannot authorize production"):
        replace(spec, production_authority=True)

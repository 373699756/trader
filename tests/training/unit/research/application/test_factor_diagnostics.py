from __future__ import annotations

from dataclasses import replace

import pytest

from tests.helpers.training.research.application.historical_extraction import _Evaluator, _WindowPort



from trader.training.application.extraction import HistoricalExtractor

from trader.training.application.factor_diagnostic_report import (
    FactorDiagnosticDimensionRecord,
    FactorDiagnosticDimensions,
)

from trader.training.application.factor_diagnostics import NativeFactorDiagnosticEvaluator


from trader.training.application.replay import HistoricalBaselineReplayer


from tests.helpers.training.research.application.factor_diagnostics import (
    _GenericReplayEvaluator,
    _evidence,
)


def test_native_factor_report_covers_metrics_and_binds_r2_r3_evidence() -> None:
    extraction, baseline, dimensions = _evidence()

    report = NativeFactorDiagnosticEvaluator().evaluate(extraction, baseline, dimensions)

    assert report.status == "exploratory"
    assert report.extraction_hash == extraction.content_hash
    assert report.baseline_report_hash == baseline.report_hash
    assert report.dimension_hash == dimensions.content_hash
    assert report.production_authority is False
    assert report.cost_rates == (0.002, 0.005, 0.01)
    assert report.decay_lags == (1, 3, 5)
    alpha = next(item for item in report.factors if item.factor_name == "alpha")
    missing = next(item for item in report.factors if item.factor_name == "missing_factor")
    assert alpha.coverage == 1.0
    assert alpha.missing_rate == 0.0
    assert alpha.mean_ic == pytest.approx(1.0)
    assert alpha.mean_rank_ic == pytest.approx(1.0)
    assert alpha.icir is None
    assert alpha.cost_quintiles[0].quintile_net_excess[-1] == pytest.approx(0.055)
    assert alpha.severe_rate_by_quintile[-1] == pytest.approx(0.5)
    assert alpha.maximum_stock_contribution == pytest.approx(6 / 11)
    assert alpha.top_five_stock_contribution == 1.0
    assert {item.dimension for item in alpha.strata} == {"board", "industry", "market_cap", "liquidity"}
    assert missing.coverage == 0.0
    assert missing.missing_rate == 1.0
    assert report.oracle_recall.pre_pruning_recall == 1.0
    assert report.oracle_recall.post_pruning_recall == 0.5


def test_native_factor_report_rejects_mismatched_parent_and_dimension_identity() -> None:
    extraction, baseline, dimensions = _evidence()

    with pytest.raises(ValueError, match="historical replay baseline"):
        NativeFactorDiagnosticEvaluator().evaluate(extraction, replace(baseline, extraction_hash="f" * 64), dimensions)
    with pytest.raises(ValueError, match="dimension"):
        NativeFactorDiagnosticEvaluator().evaluate(extraction, baseline, replace(dimensions, extraction_hash="f" * 64))


def test_native_factor_report_only_marks_exactly_40_valid_parent_days_evaluated() -> None:
    extraction = HistoricalExtractor(_WindowPort(), _Evaluator()).extract()
    baseline = HistoricalBaselineReplayer(_GenericReplayEvaluator()).replay(extraction)
    dimensions = FactorDiagnosticDimensions(
        extraction.content_hash,
        tuple(
            FactorDiagnosticDimensionRecord(
                day.summary.trade_date,
                day.content_hash,
                day.summary.input_hash,
                item.code,
                market_cap=None,
                liquidity=None,
            )
            for day in extraction.days
            for item in day.evaluated
        ),
    )

    report = NativeFactorDiagnosticEvaluator().evaluate(extraction, baseline, dimensions)

    assert report.status == "evaluated"
    assert len(report.factors[0].days) == 40
    assert report.factors[0].coverage == 0.0

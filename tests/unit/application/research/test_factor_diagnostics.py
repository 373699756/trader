from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.application.research.test_historical_ports import TRADE_DATE, _bundle, _summary
from tests.unit.application.research.test_score_r2_extraction import _Evaluator, _WindowPort
from trader.application.research.extraction import ScoreR2HistoricalExtractor
from trader.application.research.factor_diagnostic_models import (
    FactorDiagnosticDimensionRecord,
    FactorDiagnosticDimensions,
)
from trader.application.research.factor_diagnostics import ScoreNativeFactorDiagnostics
from trader.application.research.models import HistoricalEvaluatedCandidate, HistoricalFullFieldBundle
from trader.application.research.replay import ScoreR3BaselineReplayer
from trader.application.research.replay_models import BaselineReplaySelection
from trader.domain.research.historical import ScoreComponent

_CODES = tuple(f"60000{index}" for index in range(1, 7))


class _FactorPort:
    def snapshot(self):  # noqa: ANN201
        raise AssertionError("offline extraction must not request the current snapshot")

    def is_trading_day(self, trade_date):  # noqa: ANN001, ANN201
        return trade_date == TRADE_DATE

    def read_day_summary(self, trade_date):  # noqa: ANN001, ANN201
        assert trade_date == TRADE_DATE
        template = next(item for item in _summary().candidates if item.code == "600001")
        candidates = tuple(
            replace(
                template,
                code=code,
                final_components=(
                    ScoreComponent("alpha", 0.5, float(index * 10)),
                    ScoreComponent("missing_factor", 0.5, None),
                ),
                industry=f"industry-{(index - 1) % 3}",
                production_top120=index <= 5,
            )
            for index, code in enumerate(_CODES, start=1)
        )
        return replace(_summary(), candidates=candidates)

    def load_full_fields(self, trade_date, codes):  # noqa: ANN001, ANN201
        assert trade_date == TRADE_DATE
        template = _bundle(("600001",))
        candidate = template.candidates[0]
        daily = template.daily_bars[0]
        minute = template.minute_bars[0]
        window = template.adjustment_windows[0]
        settlement = template.settlements[0]
        return HistoricalFullFieldBundle(
            trade_date,
            template.input_hash,
            codes,
            tuple(replace(candidate, code=code) for code in codes),
            tuple(replace(daily, code=code, adjustment_window_id=f"window-{code}") for code in codes),
            tuple(replace(minute, code=code) for code in codes),
            tuple(replace(window, code=code, window_id=f"window-{code}") for code in codes),
            tuple(
                replace(
                    settlement,
                    basis=replace(
                        settlement.basis,
                        code=code,
                        gross_excess_return=index / 100.0,
                        mae_atr20=-2.0 if index == 6 else -0.25,
                        turnover=0.0,
                    ),
                )
                for index, code in enumerate(codes, start=1)
            ),
            template.settlement_complete_boards,
        )


class _FactorEvaluator:
    def evaluate(self, summary, bundle):  # noqa: ANN001, ANN201
        by_code = {item.code: item for item in summary.candidates}
        return tuple(
            HistoricalEvaluatedCandidate(
                code,
                "main",
                by_code[code].industry,
                50.0 + index * 5.0,
                50.0 + index * 5.0,
                by_code[code].eligible_pools,
            )
            for index, code in enumerate(bundle.requested_codes, start=1)
        )


class _FactorReplayEvaluator:
    def replay(self, day):  # noqa: ANN001, ANN201
        production = ("600005", "600004")
        oracle = ("600006", "600005")
        return tuple(
            BaselineReplaySelection(
                item.code,
                production.index(item.code) + 1 if item.code in production else None,
                oracle.index(item.code) + 1 if item.code in oracle else None,
            )
            for item in day.evaluated
        )


class _GenericReplayEvaluator:
    def replay(self, day):  # noqa: ANN001, ANN201
        selected = tuple(
            item.code
            for item in sorted(day.evaluated, key=lambda item: (-item.final_score, -item.local_score, item.code))[:2]
        )
        return tuple(
            BaselineReplaySelection(
                item.code,
                selected.index(item.code) + 1 if item.code in selected else None,
                selected.index(item.code) + 1 if item.code in selected else None,
            )
            for item in day.evaluated
        )


def _evidence():  # noqa: ANN202
    extraction = ScoreR2HistoricalExtractor(_FactorPort(), _FactorEvaluator()).extract()
    baseline = ScoreR3BaselineReplayer(_FactorReplayEvaluator()).replay(extraction)
    day = extraction.days[0]
    dimensions = FactorDiagnosticDimensions(
        extraction.content_hash,
        tuple(
            FactorDiagnosticDimensionRecord(
                TRADE_DATE,
                day.content_hash,
                day.summary.input_hash,
                code,
                market_cap=float(index * 100),
                liquidity=float((7 - index) * 10),
            )
            for index, code in enumerate(_CODES, start=1)
        ),
    )
    return extraction, baseline, dimensions


def test_native_factor_report_covers_metrics_and_binds_r2_r3_evidence() -> None:
    extraction, baseline, dimensions = _evidence()

    report = ScoreNativeFactorDiagnostics().evaluate(extraction, baseline, dimensions)

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

    with pytest.raises(ValueError, match="R3 baseline"):
        ScoreNativeFactorDiagnostics().evaluate(extraction, replace(baseline, extraction_hash="f" * 64), dimensions)
    with pytest.raises(ValueError, match="dimension"):
        ScoreNativeFactorDiagnostics().evaluate(extraction, baseline, replace(dimensions, extraction_hash="f" * 64))


def test_native_factor_report_only_marks_exactly_40_valid_parent_days_evaluated() -> None:
    extraction = ScoreR2HistoricalExtractor(_WindowPort(), _Evaluator()).extract()
    baseline = ScoreR3BaselineReplayer(_GenericReplayEvaluator()).replay(extraction)
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

    report = ScoreNativeFactorDiagnostics().evaluate(extraction, baseline, dimensions)

    assert report.status == "evaluated"
    assert len(report.factors[0].days) == 40
    assert report.factors[0].coverage == 0.0

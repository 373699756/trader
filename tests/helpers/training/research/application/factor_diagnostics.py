from __future__ import annotations
from dataclasses import replace
from tests.helpers.training.research.application.historical_ports import TRADE_DATE, _bundle, _summary
from trader.training.application.baseline_replay_report import BaselineReplaySelection
from trader.training.application.extraction import HistoricalExtractor
from trader.training.application.factor_diagnostic_report import (
    FactorDiagnosticDimensionRecord,
    FactorDiagnosticDimensions,
)
from trader.training.application.historical_extraction_models import (
    HistoricalEvaluatedCandidate,
    HistoricalFullFieldBundle,
)
from trader.training.application.replay import HistoricalBaselineReplayer
from trader.training.domain.evaluation.historical import ScoreComponent

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
    extraction = HistoricalExtractor(_FactorPort(), _FactorEvaluator()).extract()
    baseline = HistoricalBaselineReplayer(_FactorReplayEvaluator()).replay(extraction)
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

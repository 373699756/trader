"""Build a cost-aware constrained selection report from shadow predictions."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date

from trader.application.research.cost_aware_selection_models import (
    CostAwareSelectionDay,
    CostAwareSelectionReport,
)
from trader.application.research.shadow_model_models import (
    ShadowHorizon,
    ShadowModelFamily,
    ShadowModelReport,
    ShadowPrediction,
    ShadowWindowMode,
)
from trader.domain.research.cost_aware_selection import (
    CostAwareCandidate,
    CostAwareSelectionPolicy,
    select_cost_aware,
)

_SELECTION_SPEC = {
    "identity": "score_tomorrow_cost_aware_selection",
    "utility_fields": ("gross_expected_excess", "estimated_cost"),
    "tomorrow_entry_threshold": 0.0,
    "d25_entry_threshold": 0.002,
    "d25_maintenance_threshold": 0.0,
    "top_k": 6,
    "maximum_per_industry": 2,
    "maximum_board_fraction": 0.60,
    "tomorrow_stability": False,
}
COST_AWARE_SELECTION_SPEC_HASH = hashlib.sha256(
    json.dumps(_SELECTION_SPEC, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class ScoreTomorrowCostAwareSelection:
    """Apply one frozen selection specification to every shadow-model fold."""

    def build(self, shadow: ShadowModelReport) -> CostAwareSelectionReport:
        _validate_parent(shadow)
        groups: dict[tuple[ShadowHorizon, ShadowWindowMode, date], list[ShadowPrediction]] = defaultdict(list)
        for prediction in shadow.predictions:
            groups[(prediction.horizon, prediction.window_mode, prediction.prediction_date)].append(prediction)
        days: list[CostAwareSelectionDay] = []
        for horizon in ("tomorrow", "d25"):
            for window_mode in ("expanding", "rolling"):
                prediction_dates = sorted(
                    prediction_date
                    for grouped_horizon, grouped_window, prediction_date in groups
                    if grouped_horizon == horizon and grouped_window == window_mode
                )
                for model_family in ("linear", "lightgbm"):
                    incumbents: set[str] = set()
                    for prediction_date in prediction_dates:
                        predictions = tuple(groups[(horizon, window_mode, prediction_date)])
                        result = select_cost_aware(
                            tuple(
                                _candidate(prediction, model_family, prediction.code in incumbents and horizon == "d25")
                                for prediction in predictions
                            ),
                            CostAwareSelectionPolicy(horizon=horizon),
                        )
                        day = CostAwareSelectionDay(
                            prediction_date=prediction_date,
                            horizon=horizon,
                            window_mode=window_mode,
                            model_family=model_family,
                            evaluations=result.evaluations,
                            selected_codes=result.selected_codes,
                        )
                        days.append(day)
                        if horizon == "d25":
                            incumbents = set(result.selected_codes)
        return CostAwareSelectionReport(
            parent_report_hash=shadow.content_hash,
            parent_spec_hash=shadow.spec_hash,
            selection_spec_hash=COST_AWARE_SELECTION_SPEC_HASH,
            top_k=6,
            maximum_per_industry=2,
            maximum_board_fraction=0.60,
            d25_entry_threshold=0.002,
            d25_maintenance_threshold=0.0,
            tomorrow_entry_threshold=0.0,
            days=tuple(days),
        )


def _candidate(
    prediction: ShadowPrediction,
    model_family: ShadowModelFamily,
    incumbent: bool,
) -> CostAwareCandidate:
    net_excess = prediction.linear_net_excess if model_family == "linear" else prediction.lightgbm_net_excess
    severe_probability = (
        prediction.linear_severe_probability if model_family == "linear" else prediction.lightgbm_severe_probability
    )
    return CostAwareCandidate(
        code=prediction.code,
        board=prediction.board,
        industry=prediction.industry,
        gross_expected_excess=net_excess + prediction.estimated_cost,
        estimated_cost=prediction.estimated_cost,
        severe_loss_probability=severe_probability,
        uncertainty=prediction.uncertainty,
        incumbent=incumbent,
    )


def _validate_parent(shadow: ShadowModelReport) -> None:
    if shadow.schema_version != "score_tomorrow_shadow_report":
        raise ValueError("cost-aware selection requires the fixed shadow report schema")
    if shadow.status != "exploratory" or shadow.production_authority:
        raise ValueError("cost-aware selection requires an exploratory shadow report")
    prediction_keys = tuple(
        (item.prediction_date, item.horizon, item.window_mode, item.code) for item in shadow.predictions
    )
    if len(prediction_keys) != len(set(prediction_keys)):
        raise ValueError("cost-aware parent contains duplicate predictions")


__all__ = ["COST_AWARE_SELECTION_SPEC_HASH", "ScoreTomorrowCostAwareSelection"]

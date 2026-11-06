"""Persist the evaluated estimator separately from the final mature-data refit."""

from datetime import date

from trader.training.domain.final_fit import FINAL_FIT_CONTRACT, FinalFitDatePlan
from trader.training.domain.training_dates import TrainingDateSplit
from trader.training.infra.model_fitting_results import FittedIndustryModel


def BuildFinalFitEvidence(
    plan: FinalFitDatePlan,
    split: TrainingDateSplit,
    diagnostic_model: dict[str, object],
    model_hash: str,
    models: tuple[FittedIndustryModel, ...],
    calendar_dates: tuple[date, ...],
) -> dict[str, object]:
    """A frozen calibrator is transferred; diagnostic metrics do not validate this refit."""
    return {
        "contract": FINAL_FIT_CONTRACT,
        "source_cutoff": plan.source_cutoff.isoformat(),
        "calendar_dates": [day.isoformat() for day in calendar_dates],
        "maturity_sessions": plan.maturity_sessions,
        "fit_dates": [day.isoformat() for day in plan.fit_dates],
        "plan_hash": plan.content_hash,
        "model_hash": model_hash,
        "industry_training_rows": {item.industry: item.training_rows for item in models},
        "diagnostic_model": diagnostic_model,
        "diagnostic_split_hash": split.content_hash,
        "diagnostic_dates": {
            "fitting": [day.isoformat() for day in split.model_fit_dates],
            "early_stopping": [day.isoformat() for day in split.early_stopping_dates],
            "calibration": [day.isoformat() for day in split.calibration_dates],
            "validation": [day.isoformat() for day in split.confirmation_dates],
            "count_only_holdout": [day.isoformat() for day in split.daily_proxy_holdout_dates],
        },
        "evaluation_model_role": "pre_refit_diagnostic",
        "calibration_policy": "frozen_diagnostic_model_transfer",
        "final_model_unseen_validation_passed": False,
        "research_holdout_status": "may_be_used_by_final_refit_not_unseen_for_final_model",
    }


__all__ = ["BuildFinalFitEvidence"]

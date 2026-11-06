"""Persist the evaluated estimator separately from the final mature-data refit."""

from datetime import date

from trader.training.domain.final_fit import FINAL_FIT_CONTRACT, FinalFitDatePlan
from trader.training.domain.production_diagnostic_dates import (
    PRODUCTION_DIAGNOSTIC_CONTRACT,
    ProductionDiagnosticDatePlan,
)
from trader.training.infra.model_fitting_results import FittedIndustryModel


def BuildFinalFitEvidence(
    plan: FinalFitDatePlan,
    split: ProductionDiagnosticDatePlan,
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
        "diagnostic_contract": PRODUCTION_DIAGNOSTIC_CONTRACT,
        "diagnostic_source_cutoff": split.source_cutoff.isoformat(),
        "diagnostic_maturity_sessions": split.maturity_sessions,
        "diagnostic_split_hash": split.content_hash,
        "diagnostic_dates": {
            "fitting": [day.isoformat() for day in split.fitting_dates],
            "early_stopping": [day.isoformat() for day in split.early_stopping_dates],
            "calibration": [day.isoformat() for day in split.calibration_dates],
            "validation": [day.isoformat() for day in split.validation_dates],
            "warmup": [day.isoformat() for day in split.warmup_dates],
            "embargo": [[day.isoformat() for day in days] for days in split.embargo_dates],
        },
        "evaluation_model_role": "pre_refit_diagnostic",
        "calibration_policy": "frozen_recent_diagnostic_model_transfer",
        "final_model_unseen_validation_passed": False,
        "research_holdout_status": "not_part_of_recent_production_diagnostic",
    }


__all__ = ["BuildFinalFitEvidence"]

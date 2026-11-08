"""Bounded cancellation and result projection for one standard model profile."""

from __future__ import annotations


from trader.infra.serialization.canonical import CanonicalValue, ContentHash
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.profile_training import TrainingRunResult
from trader.training.application.learned_rank_training import V1TrainingResult
from trader.training.application.training_diagnostics import TrainingOperationIssue
from trader.training.domain.training_date_coverage import TrainingDateCoverage
from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.training.domain.training_input_identity import TRAINING_INDUSTRY_POLICY
from trader.training.entrypoints.report_output import ProjectTrainingError


def ProjectProfileTrainingResult(profile: ScoringProfileId, result: TrainingRunResult) -> dict[str, object]:
    return {
        "schema_version": "profile_training_result",
        "profile": profile,
        "status": result.status,
        "run_id": result.run_id,
        "training_input_hash": result.training_input_hash,
        "reused": result.reused,
        "post_commit_issues": ProjectTrainingOperationIssues(result.post_commit_issues),
        "heads": [
            {
                "strategy": head.strategy.value,
                "status": head.status,
                "diagnostic": ProjectTrainingError(head.diagnostic) if head.diagnostic is not None else None,
                "model_hash": head.model_hash,
                "report_hash": head.report_hash,
                "training_rows": head.training_rows,
                "validation_rows": head.validation_rows,
                "failure_reasons": list(head.failure_reasons),
                "date_coverage": [ProjectTrainingDateCoverage(item) for item in head.date_coverage],
                "source_cutoff": head.final_fit.source_cutoff.isoformat() if head.final_fit is not None else None,
                "label_cutoff": head.label_cutoff.isoformat() if head.label_cutoff is not None else None,
                "final_fit_start": head.final_fit.fit_dates[0].isoformat() if head.final_fit is not None else None,
                "final_fit_end": head.final_fit.fit_dates[-1].isoformat() if head.final_fit is not None else None,
                "final_fit_sessions": len(head.final_fit.fit_dates) if head.final_fit is not None else None,
                "diagnostic_fit_start": head.diagnostic_plan.fitting_dates[0].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_fit_end": head.diagnostic_plan.fitting_dates[-1].isoformat() if head.diagnostic_plan is not None else None,
                "calibration_start": head.diagnostic_plan.calibration_dates[0].isoformat() if head.diagnostic_plan is not None else None,
                "calibration_end": head.diagnostic_plan.calibration_dates[-1].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_validation_start": head.diagnostic_plan.validation_dates[0].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_validation_end": head.diagnostic_plan.validation_dates[-1].isoformat() if head.diagnostic_plan is not None else None,
                "diagnostic_maturity_sessions": head.diagnostic_plan.maturity_sessions if head.diagnostic_plan is not None else None,
                "evaluation_model_role": "pre_refit_diagnostic" if head.final_fit is not None else None,
                "final_model_unseen_validation_passed": False,
                "coverage": [
                    {
                        "partition": item.partition,
                        "target": item.target,
                        "expected_dates": item.expected_dates,
                        "effective_dates": item.effective_dates,
                        "sample_rows": item.sample_rows,
                        "target_rows": item.target_rows,
                        "missing": [
                            {
                                "date": gap.trade_date.isoformat(),
                                "sample_rows": gap.sample_rows,
                                "candidates": gap.candidates,
                                "missing_future_returns": gap.missing_future_returns,
                            }
                            for gap in item.missing
                        ],
                    }
                    for item in head.coverage
                ],
            }
            for head in result.heads
        ],
        "prediction_return_validation_passed": False,
    }


def ProjectLearnedRankTrainingResult(result: V1TrainingResult) -> dict[str, object]:
    return {
        "schema_version": "profile_training_result",
        "profile": "v1",
        "reused": result.reused,
        "status": result.status,
        "production_authority": False,
        "prediction_return_validation_passed": False,
        "run_id": result.run_id,
        "training_input_hash": result.training_input_hash,
        "sample_database_peak_bytes": result.sample_database_peak_bytes,
        "failure_reasons": list(result.failure_reasons),
        "diagnostic": ProjectTrainingError(result.diagnostic) if result.diagnostic is not None else None,
        "date_coverage": [ProjectTrainingDateCoverage(item) for item in result.date_coverage],
        "post_commit_issues": ProjectTrainingOperationIssues(result.post_commit_issues),
        "sample_counts": CanonicalValue(result.sample_counts),
        "coverage": [
            {
                "strategy": item.strategy.value,
                "partition": item.partition,
                "expected_dates": item.expected_dates,
                "effective_dates": item.effective_dates,
                "rows": item.rows,
            }
            for item in result.coverage
        ],
        "heads": [
            {
                "strategy": head.strategy.value,
                "status": "engineering_ready",
                "model_hash": head.content_hash,
                "report_hash": ContentHash(
                    (head.diagnostic.content_hash, head.split, head.validation, head.final_fit)
                ),
                "training_rows": head.training_rows,
                "validation_rows": head.validation.predicted_rows,
                "label_cutoff": head.split.label_cutoff.isoformat(),
                "source_cutoff": head.final_fit.source_cutoff.isoformat(),
                "final_fit_start": head.final_fit.fit_dates[0].isoformat(),
                "final_fit_end": head.final_fit.fit_dates[-1].isoformat(),
                "final_fit_sessions": len(head.final_fit.fit_dates),
                "diagnostic_fit_start": head.diagnostic.split.fitting[0].isoformat(),
                "diagnostic_fit_end": head.diagnostic.split.fitting[-1].isoformat(),
                "calibration_start": head.diagnostic.split.calibration[0].isoformat(),
                "calibration_end": head.diagnostic.split.calibration[-1].isoformat(),
                "diagnostic_validation_start": head.diagnostic.split.validation[0].isoformat(),
                "diagnostic_validation_end": head.diagnostic.split.validation[-1].isoformat(),
                "diagnostic_maturity_sessions": head.diagnostic.split.maturity_sessions,
                "evaluation_model_role": "pre_refit_diagnostic",
                "diagnostic_model_hash": head.diagnostic.content_hash,
                "calibration_transfer": True,
                "final_model_unseen_validation_passed": False,
                "mean_absolute_error": head.validation.mean_absolute_error,
                "mean_daily_rank_ic": head.validation.mean_daily_rank_ic,
                "mean_daily_ndcg_at_6": head.validation.mean_daily_ndcg_at_6,
                "prediction_return_validation_passed": False,
            }
            for head in result.heads
        ],
    }


def ProjectTrainingOperationIssues(issues: tuple[TrainingOperationIssue, ...]) -> list[dict[str, object]]:
    return [
        {"stage": issue.stage, "error_type": issue.error_type, "cause_type": issue.cause_type,
         "diagnostic": ProjectTrainingError(issue.diagnostic) if issue.diagnostic is not None else None}
        for issue in issues
    ]


def ProjectTrainingDateCoverage(coverage: TrainingDateCoverage) -> dict[str, object]:
    return {
        "strategy": coverage.strategy.value,
        "partition": coverage.partition,
        "basis": coverage.basis,
        "source_cutoff": coverage.source_cutoff.isoformat(),
        "label_cutoff": coverage.label_cutoff.isoformat(),
        "expected_dates": coverage.expected_dates,
        "effective_dates": coverage.effective_dates,
        "required_dates": [day.isoformat() for day in coverage.required_dates],
        "available_dates": [day.isoformat() for day in coverage.available_dates],
        "missing_dates": [day.isoformat() for day in coverage.missing_dates],
    }


def TrainingDateCoverageSummary(coverage: tuple[TrainingDateCoverage, ...]) -> str:
    names = {"fitting": "诊断拟合", "early_stopping": "早停", "calibration": "校准",
             "validation": "验证", "final_fitting": "最终拟合"}
    details = []
    for item in coverage:
        missing = item.missing_dates
        if not missing:
            continue
        head = "明日" if item.strategy.value == "tomorrow" else "D25"
        available = "至多" if item.basis == "metadata_upper_bound" else "有效"
        dates = "、".join(day.isoformat() for day in missing[:2])
        if len(missing) > 2:
            dates += f"等{len(missing)}日"
        details.append(f"{head}·{names[item.partition]} {available}{item.effective_dates}/{item.expected_dates}日（缺{dates}）")
    return "；".join(details)


def TrainingDateRangeSummary(coverage: tuple[TrainingDateCoverage, ...]) -> str:
    details = []
    for item in coverage:
        if item.partition != "final_fitting" or not item.required_dates:
            continue
        head = "明日" if item.strategy.value == "tomorrow" else "D25"
        available = "至多" if item.basis == "metadata_upper_bound" else "有效"
        details.append(
            f"{head} 资料截止{item.source_cutoff} 标签成熟截止{item.label_cutoff} "
            f"拟合{item.required_dates[0]}至{item.required_dates[-1]} "
            f"{available}{item.effective_dates}/{item.expected_dates}日"
        )
    return "；".join(details)


def ProjectTrainingIndustryCoverage(snapshot: CurrentIndustrySnapshot) -> dict[str, object]:
    return {
        "policy": TRAINING_INDUSTRY_POLICY,
        "classification": snapshot.classification,
        "source": snapshot.source,
        "industry_content_hash": snapshot.content_hash,
        "publication_hash": snapshot.publication_hash,
        "received_at": snapshot.observed_at.isoformat(),
        "refresh_after": snapshot.refresh_after.isoformat(),
        "classified_codes": len(snapshot.members),
        "missing_codes": len(snapshot.gaps),
        "gaps": [{"code": item.code, "reason": item.reason} for item in snapshot.gaps],
    }


__all__ = [
    "ProjectProfileTrainingResult", "ProjectLearnedRankTrainingResult", "ProjectTrainingOperationIssues",
    "ProjectTrainingDateCoverage", "TrainingDateCoverageSummary",
    "ProjectTrainingIndustryCoverage", "TrainingDateRangeSummary",
]

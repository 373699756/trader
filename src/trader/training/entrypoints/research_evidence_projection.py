"""Explicit public field projections for offline research commands."""

from typing import TYPE_CHECKING

from trader.training.application.capability_completion import CapabilityExecution, CapabilityPublication
from trader.training.application.cross_strategy_conclusion import CrossStrategyConclusion
from trader.training.application.h1_point_in_time_completion import H1ResearchCompletion
from trader.training.domain.evaluation.h1_point_in_time import H1CapabilityAuditReport
from trader.training.domain.evaluation.historical_industry_facts import (
    HistoricalIndustryDatasetReport,
    HistoricalIndustrySourceAudit,
)
from trader.training.domain.evaluation.point_in_time_data_qualification import PointInTimeDataQualificationReport
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutReport

if TYPE_CHECKING:
    from trader.training.entrypoints.research_evidence import ResearchEvidenceResult


def project_evidence_result(
    report: ResearchEvidenceResult, *, include_details: bool = False
) -> tuple[dict[str, object], int]:
    if isinstance(report, CapabilityExecution):
        return project_capability(report.capability, report.completion, report.publication), 1
    if isinstance(report, PointInTimeDataQualificationReport):
        return project_point_in_time_data_qualification(report), 0 if report.state == "qualified" else 1
    if isinstance(report, HistoricalIndustryDatasetReport):
        return project_historical_industry_report(
            report, include_details=include_details
        ), 0 if report.status == "qualified" else 1
    return project_holdout(report), 0 if report.status == "historical_validated" else 1


def project_capability(
    capability: H1CapabilityAuditReport,
    completion: H1ResearchCompletion,
    index: CapabilityPublication,
) -> dict[str, object]:
    return {
        "schema_version": "h1_capability_execution",
        "status": completion.status,
        "capability_hash": capability.content_hash,
        "completion_hash": completion.content_hash,
        "terminal_index_hash": index.terminal_index_hash,
        "probe_failures": list(capability.probe_failures),
        "sources": [
            {
                "source": item.source,
                "earliest_available": item.earliest_available.isoformat() if item.earliest_available else None,
                "returned_history_rows": item.page_size,
                "supports_1450": item.supports_1450,
                "effective_security_state": item.security_state_effective_at,
                "estimated_requests": item.estimated_requests,
            }
            for item in capability.probes
        ],
        "strategies": [
            {
                "strategy": item.strategy,
                "state": item.state,
                "failure_reasons": list(item.failure_reasons),
                "terminal_holdout_opened": item.terminal_holdout_opened,
            }
            for item in capability.strategies
        ],
        "residual_terminal_hashes": [list(item) for item in index.residual_terminal_hashes],
        "daily_close_selection_hash": index.daily_close_selection_hash,
        "oof_generated": completion.daily_close_selection.oof_artifact_hash is not None,
        "model_generated": completion.daily_close_selection.candidate_model_artifact_hash is not None,
        "production_authority": False,
        "automatic_model_update": False,
    }


def project_point_in_time_data_qualification(
    report: PointInTimeDataQualificationReport,
) -> dict[str, object]:
    daily = report.daily_archive
    return {
        "schema_version": report.schema_version,
        "status": report.state,
        "content_hash": report.content_hash,
        "failure_reasons": list(report.failure_reasons),
        "daily_archive": {
            "status": daily.state,
            "sessions": daily.sessions,
            "universe_count": daily.universe_count,
            "completed_codes": daily.completed_codes,
            "failed_codes": daily.failed_codes,
            "coverage_status": daily.coverage_status,
            "manifest_hash": daily.manifest_hash,
            "failure_reasons": list(daily.failure_reasons),
        },
        "industry_sources": [
            {
                "source": item.source,
                "status": item.state,
                "sampled_codes": item.sampled_codes,
                "required_sample_codes": item.required_sample_codes,
                "code_available": item.code_available,
                "industry_available": item.industry_available,
                "classification_available": item.classification_available,
                "effective_from_available": item.effective_from_available,
                "effective_to_available": item.effective_to_available,
                "queried_at_available": item.queried_at_available,
                "source_identity_available": item.source_identity_available,
                "source_evidence_hash": item.source_evidence_hash,
                "failure_reasons": list(item.failure_reasons),
            }
            for item in report.industry_sources
        ],
        "minute_sources": [
            {
                "source": item.source,
                "status": item.state,
                "earliest_available": item.earliest_available.isoformat()
                if item.earliest_available is not None
                else None,
                "sampled_codes": item.sampled_codes,
                "matched_codes": item.matched_codes,
                "sampled_trade_dates": item.sampled_trade_dates,
                "matched_trade_dates": item.matched_trade_dates,
                "coverage_ratio": item.coverage_ratio,
                "timezone": item.timezone,
                "supports_1450": item.supports_1450,
                "volume_available": item.volume_available,
                "amount_available": item.amount_available,
                "raw_qfq_pair_available": item.raw_qfq_pair_available,
                "corporate_action_semantics_proven": item.corporate_action_semantics_proven,
                "source_evidence_hash": item.source_evidence_hash,
                "failure_reasons": list(item.failure_reasons),
            }
            for item in report.minute_sources
        ],
        "point_in_time_parity": report.point_in_time_parity,
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "production_authority": report.production_authority,
    }


def project_historical_industry_report(
    report: HistoricalIndustryDatasetReport,
    *,
    include_details: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": report.schema_version,
        "status": report.status,
        "daily_manifest_hash": report.daily_manifest_hash,
        "dataset_hash": report.dataset_hash,
        "content_hash": report.content_hash,
        "eligible_codes": len(report.eligible_codes),
        "merged_fact_hash_count": len(report.merged_fact_hashes),
        "cross_source_conflicts": report.cross_source_conflicts,
        "failure_reasons": list(report.failure_reasons),
        "training_authority": report.training_authority,
        "production_authority": report.production_authority,
        "sources": [_project_source(item, include_details=include_details) for item in report.sources],
    }
    if include_details:
        payload["merged_fact_hashes"] = list(report.merged_fact_hashes)
    return payload


def _project_source(source: HistoricalIndustrySourceAudit, *, include_details: bool) -> dict[str, object]:
    contract = source.contract
    payload: dict[str, object] = {
        "source": source.source,
        "source_version": source.source_version,
        "status": source.status,
        "sampled_codes": source.sampled_codes,
        "required_sample_codes": source.required_sample_codes,
        "eligible_codes": len(source.eligible_codes),
        "actual_trading_dates": source.actual_trading_dates,
        "covered_trading_dates": source.covered_trading_dates,
        "coverage_ratio": source.coverage_ratio,
        "complete_codes": source.complete_codes,
        "incomplete_codes": source.incomplete_codes,
        "missing_dates": source.missing_dates,
        "conflict_dates": source.conflict_dates,
        "time_travel_dates": source.time_travel_dates,
        "board_counts": dict(source.board_counts),
        "cohort_counts": dict(source.cohort_counts),
        "code_available": contract.code_available,
        "industry_available": contract.industry_available,
        "classification_available": contract.classification_available,
        "effective_from_available": contract.effective_from_available,
        "effective_to_available": contract.effective_to_available,
        "queried_at_available": contract.queried_at_available,
        "source_identity_available": contract.source_identity_available,
        "fact_hash_count": len(source.fact_hashes),
        "dataset_hash": source.dataset_hash,
        "failure_reasons": list(source.failure_reasons),
    }
    if include_details:
        payload["fact_hashes"] = list(source.fact_hashes)
        payload["stocks"] = [
            {
                "code": item.code,
                "board": item.board,
                "cohort": item.cohort,
                "actual_trading_dates": item.actual_trading_dates,
                "covered_trading_dates": item.covered_trading_dates,
                "missing_dates": item.missing_dates,
                "conflict_dates": item.conflict_dates,
                "time_travel_dates": item.time_travel_dates,
                "eligible": item.eligible,
                "failure_reasons": list(item.failure_reasons),
            }
            for item in source.stocks
        ]
    return payload


def project_holdout(conclusion: CrossStrategyConclusion) -> dict[str, object]:
    reports = (conclusion.tomorrow, conclusion.d25)
    return {
        "schema_version": "terminal_holdout_execution",
        "status": conclusion.status,
        "conclusion_hash": conclusion.content_hash,
        "report_hashes": [list(item) for item in conclusion.report_hashes],
        "strategies": [_report_projection(report) for report in reports],
        "production_authority": False,
    }


def _report_projection(report: TerminalHoldoutReport) -> dict[str, object]:
    return {
        "strategy": report.strategy,
        "status": report.status,
        "research_identity": report.research_identity,
        "parent_hash": report.parent_hash,
        "candidate_hash": report.candidate_hash,
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "failure_reasons": list(report.failure_reasons),
        "report_hash": report.content_hash,
    }

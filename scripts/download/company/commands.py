"""Build only the selected download/company invocation."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.download.company.options import CompanyOptions
from scripts.download.company.company_structured_field_audit import BuildCompanyStructuredPageQuery, ValidateCompanyStructuredFinancialKeys
from scripts.download.company.company_structured_window_audit import BuildCompanyStructuredWindowQuery
from scripts.download.company.company_financial_alternatives import ValidateCompanyFinancialAlternativeCodes
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.core.paths import PROJECT_ROOT


def BuildCommands(
    options: CompanyOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    if options.financial_source_records and options.action_identity != "research-source-compare":
        raise ValueError("--financial-source-records requires the sources action")
    commands: tuple[DiagnosticCommand, ...]
    if options.action_identity == "company-resume" and (
        options.company_evidence_database is None or options.company_collection_database is None
        or options.company_replay_directory is not None
    ):
        from trader.infra.settings import LoadRuntimeSettings

        runtime = LoadRuntimeSettings(options.runtime_config)
        paths = runtime.data_paths
        if options.company_replay_directory is not None:
            RequireIsolatedReplay(options.company_replay_directory, (
                PROJECT_ROOT, runtime.project_root, paths.root, runtime.runtime_dir,
            ))
        options = replace(
            options,
            company_evidence_database=options.company_evidence_database or paths.company,
            company_collection_database=options.company_collection_database or paths.company_collection,
        )
    if options.action_identity == "company-resume":
        commands = (DiagnosticCommand(
            ("company_field_replay" if options.company_field_replay_keys else
             "company_field_material" if options.company_material_keys else "company_resume"),
            (
                python_executable,
                "-m",
                "scripts.download.company.company_resume",
                "--config", str(options.runtime_config),
                "--database",
                str(options.company_collection_database),
                "--facts-database",
                str(options.company_evidence_database),
                *(("--directory", str(options.company_replay_directory)) if options.company_replay_directory else ()),
                "--budget-seconds",
                str(options.company_audit_budget_seconds),
                *(("--audit-only",) if options.company_query_audit else ()),
                *(("--database-only",) if options.company_database_audit else ()),
                *(("--consumer-only",) if options.company_consumer_audit else ()),
                *(("--consumer-http",) if options.company_consumer_http else ()),
                *(("--source-qualification-only",) if options.company_source_qualification_audit else ()),
                *(("--field-proof-only",) if options.company_field_proof_audit else ()),
                *(("--field-proof-recovery-only",) if options.company_field_proof_recovery else ()),
                *(("--field-proof-recovery-manifest", str(options.company_field_proof_recovery_manifest)) if options.company_field_proof_recovery_manifest is not None else ()),
                *(("--risk-baseline", str(options.company_risk_baseline)) if options.company_risk_baseline else ()),
                *(("--pagination-only",) if options.company_pagination_audit else ()),
                *(("--pipeline-only",) if options.company_pipeline_audit else ()),
                *(("--pacing-only",) if options.company_pacing_audit else ()),
                *(("--material-keys", *options.company_material_keys) if options.company_material_keys else ()),
                *(("--field-replay-keys", *options.company_field_replay_keys) if options.company_field_replay_keys else ()),
                *(("--replay-observed-at", options.company_replay_observed_at.isoformat()) if options.company_replay_observed_at is not None else ()),
                *(("--recovered-text", str(options.company_recovered_text)) if options.company_recovered_text else ()),
                *(("--ocr-only",) if options.company_ocr_audit else ()),
                *(("--document-only",) if options.company_document_audit else ()),
                *(("--directory-only",) if options.company_directory_audit else ()),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "research-source-compare":
        if options.financial_alternatives:
            ValidateCompanyFinancialAlternativeCodes(options.codes)
        if options.structured_window is not None:
            if options.structured_page is not None or options.codes or options.financial_source_records or options.structured_report_date is not None:
                raise ValueError("--structured-window cannot select stock keys or another source mode")
            BuildCompanyStructuredWindowQuery(
                options.structured_window, options.structured_start, options.structured_end,
                observed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            )
        if options.financial_source_records and (
            options.structured_page != "financial" or options.codes or options.structured_report_date is not None
        ):
            raise ValueError("--financial-source-records requires --structured-page financial without key filters")
        if options.structured_page is not None and (options.structured_start is None or options.structured_end is None):
            raise ValueError("--structured-page requires explicit --structured-start and --structured-end")
        if options.structured_page is not None:
            query = BuildCompanyStructuredPageQuery(
                options.structured_page, options.structured_start, options.structured_end,
                observed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            )
            ValidateCompanyStructuredFinancialKeys(query, options.codes, options.structured_report_date)
        elif options.structured_report_date is not None:
            raise ValueError("--structured-report-date requires --structured-page financial")
        commands = (DiagnosticCommand(
            ("company_annual_income_source" if options.annual_income_period is not None else
             "company_structured_field_sampling" if options.field_sources_only else "research_source_compare"),
            (
                python_executable,
                "-m",
                "scripts.download.company.research_source_compare",
                "--config",
                str(options.runtime_config),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
                "--source-loop-seconds",
                str(options.market_source_loop_seconds),
                "--task-seconds",
                str(options.market_task_seconds),
            )
            + (("--field-sources-only", "--field-source-rows", str(options.field_source_rows)) if options.field_sources_only else ())
            + (("--field-source-schema",) if options.field_source_schema else ())
            + (("--field-source-identities",) if options.field_source_identities else ())
            + (("--financial-source-records",) if options.financial_source_records else ())
            + (("--financial-alternatives", "--codes", *options.codes) if options.financial_alternatives else ())
            + (("--structured-window", options.structured_window,
                "--structured-start", options.structured_start.isoformat(),
                "--structured-end", options.structured_end.isoformat())
               if options.structured_window is not None and options.structured_start is not None and options.structured_end is not None else ())
            + (("--structured-page", options.structured_page,
                "--structured-start", options.structured_start.isoformat(),
                "--structured-end", options.structured_end.isoformat())
               if options.structured_page is not None and options.structured_start is not None and options.structured_end is not None else ())
            + (("--codes", *options.codes, "--structured-report-date", options.structured_report_date.isoformat())
               if options.structured_page is not None and options.structured_report_date is not None else ())
            + (("--codes", *options.codes, "--annual-income-period", options.annual_income_period.isoformat()) if options.annual_income_period is not None else ())
            + (("--codes", *options.codes, "--financial-revision-period", options.financial_revision_period.isoformat()) if options.financial_revision_period is not None else ())
            + (("--official-financial-baseline", str(options.official_financial_baseline), "--official-financial-directory",
                str(options.official_financial_directory)) if options.official_financial_baseline is not None else ())
            + (("--st-name-history", "--codes", *options.codes, "--st-rounds", str(options.st_rounds),
                "--st-workers", str(options.st_workers)) if options.st_name_history else ()),
            min(common_timeout, options.market_task_seconds),
        ),)
    else:
        raise ValueError("unsupported download/company action")
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)

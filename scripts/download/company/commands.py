"""Build only the selected download/company invocation."""

from __future__ import annotations

import sys
from dataclasses import replace

from scripts.core.catalog import WorkerArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.download.company.options import CompanyOptions
from scripts.download.company.output_paths import RequireIsolatedReplay
from scripts.core.paths import PROJECT_ROOT


def BuildCommands(
    options: CompanyOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
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
            "company_resume",
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
                *(("--risk-baseline", str(options.company_risk_baseline)) if options.company_risk_baseline else ()),
                *(("--pagination-only",) if options.company_pagination_audit else ()),
                *(("--pipeline-only",) if options.company_pipeline_audit else ()),
                *(("--pacing-only",) if options.company_pacing_audit else ()),
                *(("--io-candidates-only",) if options.company_io_candidates_audit else ()),
                *(("--material-keys", *options.company_material_keys) if options.company_material_keys else ()),
                *(("--field-replay-keys", *options.company_field_replay_keys) if options.company_field_replay_keys else ()),
                *(("--ocr-only",) if options.company_ocr_audit else ()),
                *(("--document-only",) if options.company_document_audit else ()),
                *(("--directory-only",) if options.company_directory_audit else ()),
            ),
            common_timeout,
        ),)
    elif options.action_identity == "research-source-compare":
        commands = (DiagnosticCommand(
            "research_source_compare",
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
            + (("--codes", *options.codes, "--annual-income-period", options.annual_income_period.isoformat()) if options.annual_income_period is not None else ())
            + (("--codes", *options.codes, "--financial-revision-period", options.financial_revision_period.isoformat()) if options.financial_revision_period is not None else ())
            + (("--st-name-history", "--codes", *options.codes, "--st-rounds", str(options.st_rounds),
                "--st-workers", str(options.st_workers)) if options.st_name_history else ()),
            min(common_timeout, options.market_task_seconds),
        ),)
    else:
        raise ValueError("unsupported download/company action")
    return tuple(replace(command, argv=WorkerArguments(command.argv)) for command in commands)

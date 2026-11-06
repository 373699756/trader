"""Parse only the selected download/company action and validate its inputs."""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT, ExternalPath
from scripts.download.company.options import CompanyOptions
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from scripts.download.market.market_factor_budget import MARKET_SOURCE_LOOP_LIMIT_SECONDS, MARKET_TASK_LIMIT_SECONDS


def BuildParser(
    action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()
) -> argparse.ArgumentParser:
    if action_identity not in ("company-resume","research-source-compare",):
        raise ValueError("unsupported download/company action")
    parser = argparse.ArgumentParser(description=__doc__, parents=list(parents), allow_abbrev=False)
    if not parents:
        parser.add_argument("--command-timeout-seconds", type=float, default=180.0)
    parser.set_defaults(
        action_identity=action_identity,
        command_timeout_seconds=180.0,
        runtime_config=PROJECT_ROOT / 'config/runtime.json',
        company_evidence_database=None,
        company_collection_database=None,
        company_replay_directory=None,
        company_query_audit=False,
        company_database_audit=False,
        company_field_migration_audit=False,
        company_pagination_audit=False,
        company_pipeline_audit=False,
        company_pacing_audit=False,
        company_io_candidates_audit=False,
        company_material_keys=(),
        company_field_replay_keys=(),
        company_audit_budget_seconds=None,
        company_ocr_audit=False,
        company_document_audit=False,
        company_directory_audit=False,
        codes=(),
        source_timeout_seconds=4.5,
        market_source_loop_seconds=120.0,
        market_task_seconds=600.0,
        field_sources_only=False,
        field_source_rows=5,
        field_source_schema=False,
        field_source_identities=False,
        annual_income_period=None,
    )
    parser.add_argument('--runtime-config', default=str(PROJECT_ROOT / 'config/runtime.json'), help='runtime JSON configuration')
    if action_identity in ("company-resume",):
        parser.add_argument('--company-evidence-database', type=Path, default=None, help='published company evidence database (read-only)')
    if action_identity in ("company-resume",):
        parser.add_argument('--company-collection-database', type=Path, default=None, help='raw company collection index (read-only source)')
    if action_identity in ("company-resume",):
        parser.add_argument('--company-replay-directory', type=Path, help='explicit repository-external directory for an isolated company backup and resumable evidence')
    if action_identity == "company-resume":
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument('--company-query-audit', action='store_true', help='read-only bounded CNInfo query coverage audit')
        modes.add_argument('--company-database-audit', action='store_true', help='read-only local company text and retry counts; no network')
        modes.add_argument('--company-field-migration-audit', action='store_true', help='rehearse formal field schema migration only in a new external directory')
        modes.add_argument('--company-pagination-audit', action='store_true', help='read-only CNInfo page-size comparison')
        modes.add_argument('--company-pipeline-audit', action='store_true', help='isolated same-PDF serial/parallel comparison')
        modes.add_argument('--company-pacing-audit', action='store_true', help='isolated same-PDF shared/per-host pacing comparison')
        modes.add_argument('--company-io-candidates-audit', action='store_true', help='paired isolated publication candidates; never enables production settings')
        modes.add_argument('--company-material-keys', nargs='+', help='read-only source excerpts for one to six distinct source keys; requires published facts')
        modes.add_argument('--company-field-replay-keys', nargs='+', help='replay one to six source keys through real field publication only in a new external copy; no network')
        modes.add_argument('--company-ocr-audit', action='store_true', help='compare OCR on up to seven failed announcements without active writes')
        modes.add_argument('--company-document-audit', action='store_true', help='read-only failed PDF request comparison')
        modes.add_argument('--company-directory-audit', action='store_true', help='repair failed directories only in an external backup')
        parser.add_argument('--company-audit-budget-seconds', type=float, help='actual worker budget within 30..10800 seconds; wall deadline must reserve another 30 seconds')
    if action_identity in ("research-source-compare",):
        parser.add_argument('--codes', nargs='+', default=(), help='one security for --annual-income-period only; rejected by other modes')
    if action_identity in ("research-source-compare",):
        parser.add_argument('--source-timeout-seconds', type=float, default=4.5, help='vendor HTTP timeout; SDK socket timeout for BaoStock experiments')
    if action_identity in ("research-source-compare",):
        parser.add_argument('--market-source-loop-seconds', type=float, default=MARKET_SOURCE_LOOP_LIMIT_SECONDS, help='cumulative test budget per market source, capped at 120 seconds')
    if action_identity in ("research-source-compare",):
        parser.add_argument('--market-task-seconds', type=float, default=MARKET_TASK_LIMIT_SECONDS, help='hard market comparison task budget, capped at 600 seconds')
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument('--field-sources-only', action='store_true', help='bounded read-only samples of structured company field alternatives')
        modes.add_argument('--annual-income-period', type=date.fromisoformat, help='one December 31 period for exactly one A-share code')
        parser.add_argument('--field-source-rows', type=int, default=5, help='rows per structured source within 1..500; requires --field-sources-only')
        parser.add_argument('--field-source-schema', action='store_true', help='column discovery from one row; requires --field-sources-only and --field-source-rows 1')
        parser.add_argument('--field-source-identities', action='store_true', help='bounded source identities; requires --field-sources-only and at most five rows')
    return parser


def ParseOptions(args: argparse.Namespace) -> CompanyOptions:
    if (args.company_field_migration_audit or args.company_field_replay_keys) and args.company_replay_directory is None:
        raise ValueError("field migration/replay requires a new external --company-replay-directory")
    if (args.company_field_migration_audit or args.company_field_replay_keys) and args.company_replay_directory is not None:
        candidate = args.company_replay_directory.expanduser().absolute()
        if any(path.is_symlink() for path in (candidate, *candidate.parents)):
            raise ValueError("field migration output cannot contain symbolic links")
        for original in (args.company_evidence_database, args.company_collection_database):
            if original is not None:
                source = original.expanduser().absolute()
                if any(path.is_symlink() for path in (source, *source.parents)):
                    raise ValueError("field migration/replay source cannot contain symbolic links")
    options = CompanyOptions(
        action_identity=args.action_identity,
        command_timeout_seconds=args.command_timeout_seconds,
        runtime_config=Path(args.runtime_config).expanduser().resolve(),
        company_evidence_database=args.company_evidence_database.expanduser().resolve() if args.company_evidence_database is not None else None,
        company_collection_database=args.company_collection_database.expanduser().resolve() if args.company_collection_database is not None else None,
        company_replay_directory=ExternalPath(args.company_replay_directory, "--company-replay-directory"),
        company_query_audit=args.company_query_audit,
        company_database_audit=args.company_database_audit,
        company_field_migration_audit=args.company_field_migration_audit,
        company_pagination_audit=args.company_pagination_audit,
        company_pipeline_audit=args.company_pipeline_audit,
        company_pacing_audit=args.company_pacing_audit,
        company_io_candidates_audit=args.company_io_candidates_audit,
        company_material_keys=tuple(args.company_material_keys),
        company_field_replay_keys=tuple(args.company_field_replay_keys),
        company_audit_budget_seconds=(
            args.command_timeout_seconds - 30.0
            if args.company_audit_budget_seconds is None
            else args.company_audit_budget_seconds
        ),
        company_ocr_audit=args.company_ocr_audit,
        company_document_audit=args.company_document_audit,
        company_directory_audit=args.company_directory_audit,
        codes=tuple(args.codes),
        source_timeout_seconds=args.source_timeout_seconds,
        market_source_loop_seconds=args.market_source_loop_seconds,
        market_task_seconds=args.market_task_seconds,
        field_sources_only=args.field_sources_only,
        field_source_rows=args.field_source_rows,
        field_source_schema=args.field_source_schema,
        field_source_identities=args.field_source_identities,
        annual_income_period=args.annual_income_period,
    )
    if not math.isfinite(options.command_timeout_seconds) or not 0 < options.command_timeout_seconds <= 7200:
        raise ValueError("command timeout must be positive and must not exceed 7200 seconds")
    if not math.isfinite(options.source_timeout_seconds) or not 0 < options.source_timeout_seconds <= 30:
        raise ValueError("source timeout must be finite, greater than zero and at most 30 seconds")
    if not math.isfinite(options.market_source_loop_seconds) or not 0 < options.market_source_loop_seconds <= MARKET_SOURCE_LOOP_LIMIT_SECONDS:
        raise ValueError("--market-source-loop-seconds must be within 0..120")
    if not math.isfinite(options.market_task_seconds) or not 0 < options.market_task_seconds <= MARKET_TASK_LIMIT_SECONDS:
        raise ValueError("--market-task-seconds must be within 0..600")
    if sum((
        options.company_query_audit, options.company_database_audit, options.company_pagination_audit,
        options.company_pipeline_audit, options.company_ocr_audit, options.company_document_audit,
        options.company_directory_audit, options.company_pacing_audit, options.company_io_candidates_audit,
        bool(options.company_material_keys), options.company_field_migration_audit, bool(options.company_field_replay_keys),
    )) > 1:
        raise ValueError("company audit modes are mutually exclusive")
    if options.action_identity == "company-resume" and options.company_replay_directory is None and not (
        options.company_query_audit or options.company_database_audit or options.company_pagination_audit
        or options.company_document_audit or options.company_material_keys
    ):
        raise ValueError("company-resume requires --company-replay-directory")
    if options.company_material_keys and (
        len(options.company_material_keys) > 6
        or len(set(options.company_material_keys)) != len(options.company_material_keys)
        or any(not key or len(key) > 240 or any(character.isspace() for character in key) for key in options.company_material_keys)
    ):
        raise ValueError("--company-material-keys requires one to six distinct nonempty source keys without whitespace")
    if options.company_field_replay_keys and (
        not 1 <= len(options.company_field_replay_keys) <= 6
        or len(set(options.company_field_replay_keys)) != len(options.company_field_replay_keys)
        or any(not key or len(key) > 240 or any(character.isspace() for character in key) for key in options.company_field_replay_keys)
    ):
        raise ValueError("--company-field-replay-keys requires one to six distinct source keys without whitespace")
    if options.action_identity == "company-resume" and (
        not math.isfinite(options.company_audit_budget_seconds)
        or not 30 <= options.company_audit_budget_seconds <= 10800
        or options.company_audit_budget_seconds > options.command_timeout_seconds - 30
    ):
        raise ValueError("company audit budget must be within 30..10800 seconds and reserve 30 seconds before the command deadline")
    if not 1 <= options.field_source_rows <= 500 or options.field_source_rows != 5 and not options.field_sources_only:
        raise ValueError("--field-source-rows requires --field-sources-only and a value within 1..500")
    if options.field_source_schema and (not options.field_sources_only or options.field_source_rows != 1):
        raise ValueError("--field-source-schema requires --field-sources-only and --field-source-rows 1")
    if options.field_source_identities and (not options.field_sources_only or options.field_source_rows > 5):
        raise ValueError("--field-source-identities requires --field-sources-only and at most five rows")
    if options.annual_income_period is not None:
        if options.field_sources_only or len(options.codes) != 1:
            raise ValueError("--annual-income-period requires one code and cannot be combined with --field-sources-only")
        try:
            CompanyAnnualIncomeRequest(options.codes[0], (options.annual_income_period,))
        except ValueError as exc:
            raise ValueError("annual income requires an A-share code and a December 31 period") from exc
    elif options.action_identity == "research-source-compare" and options.codes:
        raise ValueError("--codes selects securities only with --annual-income-period; other modes audit market-wide sources")
    return options

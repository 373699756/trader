"""Immutable inputs owned by the download/company diagnostic adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from scripts.core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class CompanyOptions:
    action_identity: str
    command_timeout_seconds: float = 180.0
    runtime_config: Path = PROJECT_ROOT / 'config/runtime.json'
    company_evidence_database: Path | None = None
    company_collection_database: Path | None = None
    company_replay_directory: Path | None = None
    company_query_audit: bool = False
    company_database_audit: bool = False
    company_field_migration_audit: bool = False
    company_pagination_audit: bool = False
    company_pipeline_audit: bool = False
    company_pacing_audit: bool = False
    company_io_candidates_audit: bool = False
    company_material_keys: tuple[str, ...] = ()
    company_field_replay_keys: tuple[str, ...] = ()
    company_audit_budget_seconds: float = 150.0
    company_ocr_audit: bool = False
    company_document_audit: bool = False
    company_directory_audit: bool = False
    codes: tuple[str, ...] = ()
    source_timeout_seconds: float = 4.5
    market_source_loop_seconds: float = 120.0
    market_task_seconds: float = 600.0
    field_sources_only: bool = False
    field_source_rows: int = 5
    field_source_schema: bool = False
    field_source_identities: bool = False
    annual_income_period: date | None = None

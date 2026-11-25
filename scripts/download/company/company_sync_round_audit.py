"""Exercise normal composition and cancellation recovery in a new external data root."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.download.company.company_field_replay import BackupCompanyCollection
from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.bootstrap import BuildSqlitePolicy, CreateSyncProgress, ExecuteCompanySync, LoadStrategySettings
from trader.download.domain.company_sync import CompanySyncRoundScope
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.infra.settings import RuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


def PrepareCompanySyncCliAudit(
    settings: RuntimeSettings,
    directory: Path,
    *,
    request_limit: int,
    round_seconds: float,
) -> Path:
    """Prepare only a new external config/data root for the real CLI and reference preparation."""
    if (not directory.is_absolute() or directory.is_symlink()
        or type(request_limit) is not int or not 0 <= request_limit <= 512
        or not math.isfinite(round_seconds) or not 5 <= round_seconds <= 1800):
        raise ValueError("company_cli_audit_options_invalid")
    RequireIsolatedReplay(directory, (settings.project_root, settings.data_paths.root, settings.runtime_dir))
    if directory.exists():
        raise ValueError("company_cli_audit_target_exists")
    configurations = tuple(sorted(settings.config_path.parent.glob("*.json")))
    if not 1 <= len(configurations) <= 128:
        raise ValueError("company_cli_audit_configuration_count_invalid")
    for path in configurations:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("company_cli_audit_configuration_file_invalid")
    root = directory / "config"
    root.mkdir(parents=True)
    for path in configurations:
        (root / path.name).write_bytes(path.read_bytes())
    runtime = json.loads(settings.config_path.read_bytes())
    runtime["data_dir"], runtime["runtime_dir"] = str(directory / "data"), str(directory / "runtime")
    path = root / settings.config_path.name
    path.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
    download_path = root / "download.json"
    download = json.loads(download_path.read_bytes())
    download["company"]["round_request_limit"] = min(request_limit, download["company"]["round_request_limit"])
    download["company"]["round_timeout_seconds"] = min(round_seconds, download["company"]["round_timeout_seconds"])
    download_path.write_text(json.dumps(download, ensure_ascii=False), encoding="utf-8")
    return path


def AuditCompanySyncRounds(
    settings: RuntimeSettings, directory: Path, codes: tuple[str, ...], *, seconds_per_round: float,
    collection_snapshot: Path | None = None, request_limit: int | None = None,
) -> dict[str, object]:
    """Run twice with one frozen scope; independent of the active reference/universe."""
    if not 1 <= len(codes) <= 8 or len(set(codes)) != len(codes):
        raise ValueError("company_sync_round_audit_codes_invalid")
    if not directory.is_absolute() or directory.is_symlink() or not math.isfinite(seconds_per_round) or not 5 <= seconds_per_round <= 600:
        raise ValueError("company_sync_round_audit_budget_invalid")
    if request_limit is not None and (type(request_limit) is not int or not 0 <= request_limit <= 512):
        raise ValueError("company_sync_audit_request_limit_invalid")
    RequireIsolatedReplay(directory, (settings.project_root, settings.data_paths.root, settings.runtime_dir))
    if directory.exists():
        raise ValueError("company_sync_round_audit_target_exists")
    directory.mkdir(parents=True)
    isolated = replace(settings, data_paths=DataPaths(directory / "data"), runtime_dir=directory / "runtime")
    if request_limit is not None:
        configuration = json.loads((settings.project_root / "config/download.json").read_bytes())
        configuration["company"]["round_request_limit"] = min(configuration["company"]["round_request_limit"], request_limit)
        policy_root = directory / "policy"
        (policy_root / "config").mkdir(parents=True)
        (policy_root / "config/download.json").write_text(json.dumps(configuration, ensure_ascii=False), encoding="utf-8")
        isolated = replace(isolated, project_root=policy_root)
    if collection_snapshot is not None:
        _copy_directory_metadata(settings, collection_snapshot, isolated.data_paths.company_sources, directory)
    scope = CompanySyncRoundScope(tuple(sorted(codes)), datetime.now(ZoneInfo("Asia/Shanghai")))
    observations: list[dict[str, object]] = []
    for number in (1, 2):
        started = time.monotonic()
        stop_at = started + seconds_per_round
        def cancelled(stop_at: float = stop_at) -> bool:
            return time.monotonic() >= stop_at
        result = ExecuteCompanySync(
            isolated, scope, cancel_requested=cancelled,
            report=CreateSyncProgress(),
        )
        observations.append({
            "invocation": number, "round_id": result.round_id, "scope_identity": result.scope.identity,
            "cancelled": result.cancelled, "budget_exhausted": result.budget_exhausted,
            "processing_complete": result.processing_complete, "business_complete": result.business_complete,
            "publication_hash": result.publication_hash, "elapsed_seconds": time.monotonic() - started,
            "families": [{
                "family": item.family.value, "steps": item.steps, "accepted_inputs": item.accepted_inputs,
                "reserved_source_calls": item.source_calls, "unknown_attempts": item.unknown_attempts,
                "processed_scope_complete": item.result.processed_scope_complete, "can_continue": item.result.can_continue,
                "gaps": list(item.result.gaps), "failure": item.result.failure,
            } for item in result.families],
        })
    return {
        "probe": "normal_company_rounds", "isolated_sample": True, "writes_current_company_data": False,
        "reference_preparation_included": False, "eligible_population_verified": False,
        "production_adopted": False, "field_correctness_verified": False,
        "scope_identity": scope.identity, "codes": list(scope.codes), "cutoff": scope.cutoff.isoformat(),
        "runtime_configuration_sha256": hashlib.sha256(settings.config_path.read_bytes()).hexdigest(),
        "seconds_per_round": seconds_per_round, "rounds": observations,
        "metadata_snapshot_copied": collection_snapshot is not None, "request_limit_requested": request_limit,
        "company_configuration_sha256": hashlib.sha256((isolated.project_root / "config/download.json").read_bytes()).hexdigest(),
        "same_finite_round": bool(observations[0]["round_id"]) and observations[0]["round_id"] == observations[1]["round_id"],
    }


def _copy_directory_metadata(settings: RuntimeSettings, source: Path, target: Path, directory: Path) -> None:
    if not source.is_absolute() or source.is_symlink():
        raise ValueError("company_sync_audit_source_invalid")
    RequireIsolatedReplay(directory, (source.parent,))
    policy = BuildSqlitePolicy(settings)
    if not BackupCompanyCollection(source, target, policy, time.monotonic() + 15, material_only=True):
        raise ValueError("company_sync_audit_source_changed_during_copy")
    with SqliteSession(target, SqliteConnectionOptions(read_only=True), sqlite_policy=policy) as snapshot:
        if snapshot.run("SELECT 1 FROM disclosures WHERE body_retained=1 LIMIT 1").fetchone() is not None or snapshot.run(
            "SELECT 1 FROM document_tasks WHERE path IS NOT NULL LIMIT 1").fetchone() is not None:
            raise ValueError("company_sync_audit_requires_directory_only_sample")


def AuditCompanySyncRestart(
    settings: RuntimeSettings, directory: Path, scope: CompanySyncRoundScope, *, seconds: float,
    requested_cutoff: datetime | None = None,
) -> dict[str, object]:
    """Explicitly re-enter an existing isolated round; never create a successor scope."""
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir() or not math.isfinite(seconds) or not 5 <= seconds <= 60:
        raise ValueError("company_sync_restart_audit_options_invalid")
    RequireIsolatedReplay(directory, (settings.project_root, settings.data_paths.root, settings.runtime_dir))
    isolated = replace(settings, data_paths=DataPaths(directory / "data"), runtime_dir=directory / "runtime")
    policy = BuildSqlitePolicy(isolated)
    reader = SQLiteCompanyEvidenceReader(isolated.data_paths.company,
        event_policy=LoadStrategySettings(isolated.strategy_config_path).company_scoring.events, sqlite_policy=policy)
    journal = SQLiteCompanySyncRoundJournal(isolated.data_paths.company_sync, sqlite_policy=policy,
        publication_identity=reader.publication_identity)
    active = journal.active()
    if active is None or active.scope != scope:
        raise ValueError("company_sync_restart_audit_scope_mismatch")
    before_calls, before_publication = journal.source_calls(active.round_id), reader.publication_identity()
    if requested_cutoff is not None and (
        requested_cutoff.utcoffset() is None or requested_cutoff < scope.cutoff
        or requested_cutoff.astimezone(scope.cutoff.tzinfo).date() != scope.cutoff.date()
        or before_calls < active.max_source_calls
    ):
        raise ValueError("company_sync_restart_audit_cutoff_invalid")
    requested_scope = CompanySyncRoundScope(scope.codes, requested_cutoff or scope.cutoff)
    stop_at = time.monotonic() + seconds
    result = ExecuteCompanySync(isolated, requested_scope, cancel_requested=lambda: time.monotonic() >= stop_at,
        report=CreateSyncProgress())
    calls = tuple(item.source_calls for item in result.families)
    after_calls = sum(value for value in calls if value is not None) if len(calls) == len(scope.families) and all(value is not None for value in calls) else None
    return {
        "probe": "normal_company_restart", "isolated_sample": True, "writes_current_company_data": False,
        "same_finite_round": result.round_id == active.round_id and result.scope == scope,
        "scope_identity": result.scope.identity, "before_reserved_slots": before_calls, "after_reserved_slots": after_calls,
        "requested_cutoff": requested_scope.cutoff.isoformat(),
        "no_new_source_calls": after_calls == before_calls, "cancelled": result.cancelled,
        "processing_complete": result.processing_complete, "business_complete": result.business_complete,
        "publication_unchanged": bool(before_publication) and before_publication == result.publication_hash,
        "production_adopted": False, "field_correctness_verified": False,
    }

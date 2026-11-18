"""Exercise normal composition and cancellation recovery in a new external data root."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.bootstrap import BuildSqlitePolicy, CreateSyncProgress, ExecuteCompanySync, LoadStrategySettings
from trader.download.domain.company_sync import CompanySyncRoundScope
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.infra.settings import RuntimeSettings
from trader.infra.settings.data_paths import DataPaths


def AuditCompanySyncRounds(
    settings: RuntimeSettings, directory: Path, codes: tuple[str, ...], *, seconds_per_round: float,
) -> dict[str, object]:
    """Run twice with one frozen scope; independent of the active reference/universe."""
    if not 1 <= len(codes) <= 8 or len(set(codes)) != len(codes):
        raise ValueError("company_sync_round_audit_codes_invalid")
    if not directory.is_absolute() or directory.is_symlink() or not math.isfinite(seconds_per_round) or not 5 <= seconds_per_round <= 600:
        raise ValueError("company_sync_round_audit_budget_invalid")
    RequireIsolatedReplay(directory, (settings.project_root, settings.data_paths.root, settings.runtime_dir))
    if directory.exists():
        raise ValueError("company_sync_round_audit_target_exists")
    directory.mkdir(parents=True)
    isolated = replace(settings, data_paths=DataPaths(directory / "data"), runtime_dir=directory / "runtime")
    scope = CompanySyncRoundScope(tuple(sorted(codes)), datetime.now(ZoneInfo("Asia/Shanghai")))
    observations: list[dict[str, object]] = []
    for number in (1, 2):
        started = time.monotonic()
        stop_at = started + seconds_per_round
        result = ExecuteCompanySync(
            isolated, scope, cancel_requested=lambda stop_at=stop_at: time.monotonic() >= stop_at,
            report=CreateSyncProgress(), fill=False,
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
        "same_finite_round": bool(observations[0]["round_id"]) and observations[0]["round_id"] == observations[1]["round_id"],
    }


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
    requested_scope = scope if requested_cutoff is None else CompanySyncRoundScope(scope.codes, requested_cutoff, scope.families)
    stop_at = time.monotonic() + seconds
    result = ExecuteCompanySync(isolated, requested_scope, cancel_requested=lambda: time.monotonic() >= stop_at,
        report=CreateSyncProgress(), fill=False)
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

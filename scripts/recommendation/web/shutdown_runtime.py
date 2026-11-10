"""Validate an explicitly owned production run, without controlling external services."""

from __future__ import annotations

import hashlib
import math
import os
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from scripts.core.contracts import ToolOutcome
from scripts.core.paths import PROJECT_ROOT
from scripts.recommendation.web.shutdown import ObserveShutdown, ShutdownObservationOptions
from trader.infra.http_contracts import HttpError, HttpTransport
from trader.infra.linux_process import ReadLinuxProcessGroupMembers, ReadLinuxProcessIdentity
from trader.infra.process_family import ProcessFamily
from trader.infra.settings import LoadRuntimeSettings
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords

ShutdownTrigger = Literal["serving", "acquisition", "model", "publication", "frozen"]


@dataclass(frozen=True)
class OwnedShutdownOptions:
    runtime_config: Path
    shutdown_log: Path
    trigger: ShutdownTrigger
    startup_seconds: float
    observation_seconds: float
    profiles: tuple[ScoringProfileId, ...] = SCORING_PROFILE_IDS

    def __post_init__(self) -> None:
        if self.trigger not in {"serving", "acquisition", "model", "publication", "frozen"}:
            raise ValueError("owned_shutdown_trigger_invalid")
        if not math.isfinite(self.startup_seconds) or not 0 < self.startup_seconds <= 900:
            raise ValueError("owned_shutdown_startup_timeout_invalid")
        if not math.isfinite(self.observation_seconds) or not 0 < self.observation_seconds <= 60:
            raise ValueError("owned_shutdown_observation_timeout_invalid")
        if self.shutdown_log.is_relative_to(PROJECT_ROOT):
            raise ValueError("owned_shutdown_log_requires_external_path")
        if not self.profiles or len(set(self.profiles)) != len(self.profiles) or not set(self.profiles) <= set(SCORING_PROFILE_IDS):
            raise ValueError("owned_shutdown_profiles_invalid")
        if self.trigger == "frozen" and set(self.profiles) != set(SCORING_PROFILE_IDS):
            raise ValueError("owned_shutdown_freeze_requires_all_profiles")


@dataclass(frozen=True)
class CommittedFreezeEvidence:
    profile: str
    strategy: str
    trade_date: date
    record_hash: str
    freeze_hash: str
    freeze_version: str
    commit_kind: str
    selected_count: int


@dataclass(frozen=True)
class _BusyEvidence:
    profile: str
    phase: str
    work: tuple[str, ...]
    observed_monotonic: float


def ReadCommittedFreezeEvidence(config_path: Path, trade_date: date) -> tuple[CommittedFreezeEvidence, ...]:
    """Use the formal reader's byte/hash validation; never initialize or recover a database."""
    from trader.bootstrap import BuildSqlitePolicy

    settings = LoadRuntimeSettings(config_path)
    policy = BuildSqlitePolicy(settings)
    evidence: list[CommittedFreezeEvidence] = []
    for profile in SCORING_PROFILE_IDS:
        records = SQLiteFreezeRecords(settings.data_paths.recommendation(profile) / "freeze.sqlite3", sqlite_policy=policy)
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            record = records.load(strategy, trade_date)
            if record is not None:
                evidence.append(CommittedFreezeEvidence(
                    profile, strategy.value, trade_date, hashlib.sha256(CommittedRecordBytes(record)).hexdigest(), record.freeze.content_hash,
                    record.freeze.version, record.commit_kind, len(record.freeze.items),
                ))
    return tuple(evidence)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("owned_runtime_status_invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("owned_runtime_counter_invalid")
    return value


def _busy_evidence(payload: Mapping[str, object], trigger: ShutdownTrigger) -> _BusyEvidence | None:
    if payload.get("runtime_started") is not True:
        return None
    phase, profile = payload.get("phase"), payload.get("selected_profile")
    if not isinstance(phase, str) or profile not in SCORING_PROFILE_IDS:
        raise ValueError("owned_runtime_identity_invalid")
    work: list[str] = []
    if trigger == "serving":
        work.append("http_serving")
    elif trigger == "frozen":
        strategies = _mapping(payload.get("strategies"))
        if all(
            _mapping(strategies.get(strategy)).get("frozen") is True
            and _mapping(strategies.get(strategy)).get("status") == "ready"
            for strategy in ("tomorrow", "d25")
        ):
            work.append("two_formal_heads_restored")
    elif trigger == "acquisition":
        sources = _mapping(_mapping(payload.get("market_data")).get("sources"))
        for name in ("tencent_market", "sina", "tencent", "tencent_long"):
            source = _mapping(sources.get(name))
            if _integer(source.get("physical_fetch_inflight_count")) > 0:
                work.append(name)
    elif trigger == "model":
        heads = _mapping(_mapping(payload.get("scoring_profile")).get("heads"))
        for strategy in ("tomorrow", "d25"):
            if _integer(_mapping(heads.get(strategy)).get("prediction_inflight_count")) > 0:
                work.append(strategy + ":prediction")
    else:
        operations = _mapping(payload.get("scheduler")).get("publication_io")
        if not isinstance(operations, list):
            raise ValueError("owned_runtime_publication_status_invalid")
        for raw in operations:
            operation = _mapping(raw)
            if operation.get("operation") in {"current_publish", "formal_write", "formal_publish", "freeze_seal"}:
                if _integer(operation.get("pending_count")) > 0 and operation.get("latency_ms") is None:
                    work.append(str(operation["operation"]))
    return _BusyEvidence(str(profile), phase, tuple(work), time.monotonic()) if work else None


def _wait_for_trigger(
    process: ProcessFamily, transport: HttpTransport, url: str, config_version: str, options: OwnedShutdownOptions,
) -> _BusyEvidence:
    deadline = time.monotonic() + options.startup_seconds
    frozen_profiles: set[str] = set()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("owned_runtime_exited_before_trigger")
        if options.shutdown_log.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("owned_runtime_log_limit")
        profiles = (("v5",) if "v5" in options.profiles else options.profiles) if options.trigger == "model" else options.profiles
        for profile in profiles:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = transport.get(
                    url + "/api/status?profile=" + profile, timeout=min(1.0, remaining), max_response_bytes=1024 * 1024,
                )
                response.raise_for_status()
                payload = _mapping(response.json())
                version = payload.get("runtime_version")
                if not isinstance(version, str) or not version.startswith(config_version + "+"):
                    raise RuntimeError("owned_runtime_configuration_mismatch")
                evidence = _busy_evidence(payload, options.trigger)
                if options.trigger == "model" and evidence is None and _busy_evidence(payload, "frozen") is not None:
                    raise RuntimeError("owned_runtime_model_finished_before_observation")
                if options.trigger == "frozen":
                    if evidence is not None:
                        frozen_profiles.add(profile)
                    else:
                        frozen_profiles.discard(profile)
                    if len(frozen_profiles) != len(options.profiles):
                        continue
                if evidence is not None:
                    return evidence
            except HttpError:
                continue
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise TimeoutError("owned_runtime_trigger_not_observed")


def ObserveOwnedRuntimeShutdown(options: OwnedShutdownOptions) -> ToolOutcome:
    """Launch the real entry point; only this explicitly created family is cleaned up."""
    from trader.bootstrap import OpenExternalHttp

    if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        raise ValueError("owned_runtime_pidfd_unavailable")
    settings = LoadRuntimeSettings(options.runtime_config)
    original = LoadRuntimeSettings(PROJECT_ROOT / "config/runtime.json")
    outputs = (
        settings.runtime_dir, settings.data_paths.training_maintenance,
        *(settings.data_paths.recommendation(profile) for profile in SCORING_PROFILE_IDS),
        *(settings.data_paths.research(profile) for profile in SCORING_PROFILE_IDS),
    )
    original_outputs = (
        original.runtime_dir, original.data_paths.training_maintenance,
        *(original.data_paths.recommendation(profile) for profile in SCORING_PROFILE_IDS),
        *(original.data_paths.research(profile) for profile in SCORING_PROFILE_IDS),
    )
    if any(path.resolve().is_relative_to(PROJECT_ROOT) for path in outputs) or any(
        target.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(target.resolve())
        for target in outputs for source in original_outputs
    ):
        raise ValueError("owned_runtime_requires_isolated_outputs")
    if settings.server.host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise ValueError("owned_runtime_requires_local_ipv4_binding")
    with socket.socket() as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", settings.server.port)) == 0:
            raise ValueError("owned_runtime_endpoint_in_use")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    before = ReadCommittedFreezeEvidence(options.runtime_config, today)
    process: ProcessFamily | None = None
    outcome: ToolOutcome | None = None
    evidence: _BusyEvidence | None = None
    survivors: tuple[int, ...] | None = None
    cleanup_error = None
    with options.shutdown_log.open("xb") as output:
        try:
            process = ProcessFamily(
                (str(PROJECT_ROOT / "run.sh"), ",".join(options.profiles)), cwd=PROJECT_ROOT,
                environment={
                    **os.environ, "TRADER_CONFIG": str(options.runtime_config),
                    "TRADER_HOST": settings.server.host, "TRADER_PORT": str(settings.server.port),
                },
                stdout=output, stderr=subprocess.STDOUT,
            )
            with OpenExternalHttp() as transport:
                evidence = _wait_for_trigger(
                    process, transport, f"http://127.0.0.1:{settings.server.port}", settings.config_version, options,
                )
            identity = ReadLinuxProcessIdentity(process.pid)
            if identity is None:
                raise RuntimeError("owned_runtime_identity_unavailable")
            outcome = ObserveShutdown(
                ShutdownObservationOptions(identity.pid, identity.birth_ticks, options.shutdown_log, options.observation_seconds, True),
                owned_process=process,
            )
            survivors = tuple(member.native_id for member in ReadLinuxProcessGroupMembers(process.pid))
        except (OSError, RuntimeError, ValueError) as exc:
            outcome = ToolOutcome(1, {
                "schema_version": "shutdown-observation", "status": "failed", "summary": {},
                "findings": [str(exc) if str(exc).startswith("owned_runtime_") else type(exc).__name__],
            })
        finally:
            if process is not None:
                try:
                    process.close()
                    process.close_streams()
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    cleanup_error = type(exc).__name__
    if outcome is None:
        raise RuntimeError("owned_runtime_observation_missing")
    after = ReadCommittedFreezeEvidence(options.runtime_config, today)
    unchanged = all(item in after for item in before)
    summary = dict(_mapping(outcome.payload.get("summary")))
    freeze_complete = options.trigger != "frozen" or len(after) == 8
    accepted = summary.get("process_shutdown_acceptance") is True and survivors == () and cleanup_error is None and unchanged and freeze_complete
    summary.update({
        "runtime_shutdown_acceptance": accepted and options.trigger in {"acquisition", "model", "publication"},
        "freeze_recovery_acceptance": accepted and options.trigger == "frozen" and len(after) == 8,
        "launch_run": True, "fixture": False, "trigger": options.trigger,
        "profiles": list(options.profiles),
        "busy_evidence": asdict(evidence) if evidence is not None else None,
        "remaining_process_ids_before_cleanup": list(survivors) if survivors is not None else None,
        "owned_family_cleanup_error": cleanup_error,
        "existing_freeze_record_count": len(before), "existing_freeze_records_unchanged": unchanged,
        "freeze_records": [asdict(item) | {"trade_date": item.trade_date.isoformat()} for item in after],
    })
    raw_findings = outcome.payload.get("findings")
    if not isinstance(raw_findings, list) or any(not isinstance(value, str) for value in raw_findings):
        raise ValueError("owned_runtime_findings_invalid")
    findings = [str(value) for value in raw_findings]
    if survivors:
        findings.append("owned_runtime_processes_remain_after_exit")
    if cleanup_error is not None:
        findings.append("owned_runtime_family_cleanup_failed")
    if not unchanged:
        findings.append("owned_runtime_existing_freeze_changed")
    if not freeze_complete:
        findings.append("owned_runtime_freeze_records_incomplete")
    return ToolOutcome(0 if accepted else 1, {
        "schema_version": "shutdown-observation", "status": "passed" if accepted else "failed",
        "summary": summary, "findings": findings,
    })

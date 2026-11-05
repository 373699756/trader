"""Review durable complete-training child reports and aggregate reviewed evidence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from scripts.core.contracts import DiagnosticResult
from scripts.training.jobs.training_performance import (
    SummarizeTrainingBenchmark,
    TrainingReuseFailures,
    TrainingSampleReuseFailures,
)
from trader.infra.atomic_files.json import SyncDirectory
from trader.infra.serialization.sealing import PublishImmutable

_PROFILES = ("v1", "v2", "v3", "v5")
_STRATEGIES = ("tomorrow", "d25")
_MODES = ("baseline_before", "candidate", "baseline_after")
_REPORT_LIMIT = 32 * 1024**2
_REVIEW_SCHEMA = "training-benchmark-child-review"
_IDENTITY_SCHEMA = "training-benchmark-input-identity"
_EXECUTION_FLAGS = (
    "memory_first", "release_cpu_before_publish", "shared_matrix_memory", "shared_validation", "normalized_tcn_batches",
)
_RESOURCE_FIELDS = (
    "cpu_seconds", "observed_peak_rss_bytes", "observed_peak_os_threads",
    "physical_read_bytes", "physical_write_bytes", "samples", "sampling_failures",
    "sampling_interval_seconds", "scope", "native_thread_utilization_verified",
    "filesystem_free_bytes_at_start", "filesystem_minimum_free_bytes", "filesystem_measurement_scope",
    "sampled_peak_swap_bytes", "major_page_faults", "minor_page_faults",
    "system_swap_in_pages", "system_swap_out_pages", "system_swap_scope", "page_fault_scope",
)


@dataclass(frozen=True)
class _ChildReview:
    identity: dict[str, object]
    details: dict[str, object]
    total_seconds: float
    mode: str
    numeric_failure: str | None = None


def ReviewTrainingBenchmarkChild(report: Path, *, identity_path: Path) -> dict[str, object]:
    """Verify one raw child JSON and seal the first valid common input identity.

    The caller persists the returned review separately. Failed reviews never replace
    an identity. A verified review binds the original bytes for later aggregation.
    """
    try:
        payload, report_hash = _read_document(report)
        reviewed = _review_child(payload)
        identity_file = identity_path.absolute()
        if identity_file.is_symlink():
            raise ValueError("training_benchmark_identity_symlink")
        if reviewed.numeric_failure is not None:
            if identity_file.exists():
                persisted, _identity_file_hash = _read_document(identity_file)
                _require_identity(persisted, reviewed.identity, compare_numeric=False)
            raise ValueError(reviewed.numeric_failure)
        created = PublishImmutable(identity_file, _render(reviewed.identity))
        if created:
            SyncDirectory(identity_file.parent)
        persisted, _identity_file_hash = _read_document(identity_file)
        _require_identity(persisted, reviewed.identity)
        return {
            "schema_version": _REVIEW_SCHEMA,
            "status": "verified",
            "verified": True,
            "report_path": str(report.absolute()),
            "report_sha256": report_hash,
            "identity_path": str(identity_file),
            "identity_sha256": _digest(reviewed.identity),
            "identity": reviewed.identity,
            "details": reviewed.details,
            "performance_acceptance": False,
            "production_authority": False,
        }
    except (OSError, UnicodeError, ValueError) as exc:
        return _failed("training-benchmark-child-review", _failure_reason(exc))


def SummarizeTrainingBenchmarkFiles(entries: tuple[tuple[str, Path], ...]) -> dict[str, object]:
    """Load persisted review JSON files, recheck raw bytes, then use the existing timing gate."""
    try:
        if not entries or len(entries) > 15:
            raise ValueError("training_benchmark_entries_invalid")
        results: list[DiagnosticResult] = []
        seen_reports: set[Path] = set()
        identities: set[str] = set()
        for name, review_path in entries:
            record, _record_hash = _read_document(review_path)
            if record.get("schema_version") != _REVIEW_SCHEMA or record.get("status") != "verified" or record.get("verified") is not True:
                reasons = record.get("failure_reasons")
                if (record.get("schema_version") == _REVIEW_SCHEMA and isinstance(reasons, list) and reasons
                        and all(isinstance(reason, str) and reason.startswith("training_benchmark_") for reason in reasons)):
                    raise ValueError("training_benchmark_child_review_failed:" + ",".join(reasons))
                raise ValueError("training_benchmark_child_not_reviewed")
            report = Path(_text(record.get("report_path"), "training_benchmark_report_path_missing"))
            identity_path = Path(_text(record.get("identity_path"), "training_benchmark_identity_path_missing"))
            if not report.is_absolute() or not identity_path.is_absolute() or report.resolve() in seen_reports:
                raise ValueError("training_benchmark_report_path_invalid_or_repeated")
            seen_reports.add(report.resolve())
            payload, report_hash = _read_document(report)
            if record.get("report_sha256") != report_hash:
                raise ValueError("training_benchmark_reviewed_report_changed")
            reviewed = _review_child(payload)
            identity, _identity_file_hash = _read_document(identity_path)
            identity_hash = _digest(reviewed.identity)
            if identity != reviewed.identity or record.get("identity") != identity or record.get("identity_sha256") != identity_hash:
                raise ValueError("training_benchmark_reviewed_identity_changed")
            identities.add(identity_hash)
            parts = name.split(":")
            if (len(parts) not in (3, 4) or parts[0] != "training_alignment"
                    or parts[-2] != reviewed.mode or tuple(parts[-1].split(",")) != _PROFILES):
                raise ValueError("training_benchmark_child_name_mismatch")
            if reviewed.numeric_failure is not None:
                raise ValueError(reviewed.numeric_failure)
            results.append(DiagnosticResult(name, 0, reviewed.total_seconds * 1000, payload, None))
        if len(identities) != 1:
            raise ValueError("training_benchmark_input_identity_changed")
        comparison = SummarizeTrainingBenchmark(tuple(results))
        comparable = comparison.get("comparable") is True
        return {
            **comparison,
            "schema_version": "training-benchmark-file-summary",
            "status": "verified" if comparable else "failed",
            "reviewed_children": len(results),
            "failure_reasons": [] if comparable else ["training_benchmark_children_not_comparable"],
            "performance_acceptance": False,
            "production_authority": False,
        }
    except (OSError, UnicodeError, ValueError) as exc:
        return _failed("training-benchmark-file-summary", _failure_reason(exc))


def _review_child(payload: dict[str, object]) -> _ChildReview:
    status = payload.get("status")
    if payload.get("schema_version") != "training-alignment-diagnostic" or status not in ("passed", "degraded"):
        raise ValueError("training_benchmark_child_not_passed")
    mode = _text(payload.get("benchmark_mode"), "training_benchmark_mode_missing")
    if mode not in _MODES:
        raise ValueError("training_benchmark_mode_invalid")
    summary = _mapping(payload.get("summary"), "training_benchmark_summary_missing")
    if (type(summary.get("source_data_writes")) is not int or summary.get("source_data_writes") != 0
            or summary.get("source_identity_unchanged") is not True or summary.get("source_files_unchanged") is not True):
        raise ValueError("training_benchmark_source_changed_or_unverified")
    input_mode = summary.get("input_mode")
    guard = summary.get("published_source_guard")
    if input_mode not in ("published_history_read_only", "external_history_copy"):
        raise ValueError("training_benchmark_source_mode_missing")
    if input_mode == "published_history_read_only" and guard != "sqlite_mode_ro; due_projection_persist_false; fitter_unmodified":
        raise ValueError("training_benchmark_source_guard_missing")
    if input_mode == "external_history_copy" and guard != "not_required_for_isolated_copy":
        raise ValueError("training_benchmark_source_guard_missing")
    configuration = _hash(summary.get("training_configuration_sha256"))
    implementation = _hash(summary.get("training_implementation_sha256"))
    initial = _mapping(summary.get("initial"), "training_benchmark_initial_missing")
    repeat = _mapping(summary.get("repeat"), "training_benchmark_repeat_missing")
    scenario = _mapping(summary.get("sample_reuse"), "training_benchmark_sample_reuse_missing")
    sample = _mapping(scenario.get("run"), "training_benchmark_sample_reuse_missing")
    heads = _head_values(initial, _PROFILES, reused=False)
    _head_values(repeat, _PROFILES, reused=True)
    _head_values(sample, _PROFILES[1:], reused=False)
    rows = _scan(initial, _PROFILES, first=True)
    _scan(repeat, _PROFILES, first=False)
    _scan(sample, _PROFILES[1:], first=False)
    reuse_failures = TrainingReuseFailures(initial, repeat, _PROFILES)
    sample_failures = TrainingSampleReuseFailures(initial, scenario, _PROFILES)
    if "repeat_reuse_failures" in summary and _failure_codes(summary["repeat_reuse_failures"]) != reuse_failures:
        raise ValueError("training_benchmark_evidence_invalid:reuse_reported_failures_mismatch")
    if "failures" in scenario and _failure_codes(scenario["failures"]) != sample_failures:
        raise ValueError("training_benchmark_evidence_invalid:sample_reuse_reported_failures_mismatch")
    evidence_failures = (
        *reuse_failures,
        *(failure for failure in sample_failures if failure != "sample_reuse_model_values_mismatch"),
    )
    if evidence_failures:
        raise ValueError("training_benchmark_evidence_invalid:" + ",".join(evidence_failures))
    fingerprints = _fingerprints(initial, _PROFILES)
    repeat_numeric = fingerprints != _fingerprints(repeat, _PROFILES)
    sample_numeric = [head for head in fingerprints if head["profile"] in _PROFILES[1:]] != _fingerprints(
        sample, _PROFILES[1:]
    )
    before = _cache_signatures(scenario.get("source_cache_signatures_before"))
    after = _cache_signatures(scenario.get("source_cache_signatures_after"))
    if before != after:
        raise ValueError("training_benchmark_sample_source_changed")
    if scenario.get("cache_transfer_method") == "hardlink":
        released = scenario.get("preparation_cache_released_after_verification")
        # The real diagnostic releases preparation hardlinks only after a refit with no
        # failures. A verified numeric mismatch therefore retains them by design.
        if released is not True and not (sample_numeric and released is False):
            raise ValueError("training_benchmark_hardlink_cache_not_released")
    policies = {
        key: _policy(summary.get(key), key) for key in
        ("execution_candidate", "computation_candidate", "scheduling_candidate")
    }
    if mode.startswith("baseline_") and (
        policies["execution_candidate"].get("execution") != "baseline"
        or policies["execution_candidate"].get("cpu") != "baseline"
        or policies["execution_candidate"].get("fanout", "baseline") != "baseline"
        or policies["computation_candidate"].get("name") != "baseline"
        or policies["scheduling_candidate"].get("name") != "baseline"
        or any(value is True for value in _mapping(
            policies["execution_candidate"].get("options", {}), "training_benchmark_policy_options_invalid"
        ).values())
    ):
        raise ValueError("training_benchmark_baseline_has_candidate")
    workloads: dict[str, object] = {}
    for name, run in (("initial", initial), ("repeat", repeat), ("sample_reuse", sample)):
        workloads[name] = {
            "seconds": _seconds(run.get("seconds")),
            "resources": _resources(run.get("resources")),
            "history_scan_passes": 1 if name == "initial" else 0,
            "history_rows_read": rows if name == "initial" else 0,
        }
    preparation = _mapping(scenario.get("preparation"), "training_benchmark_sample_preparation_missing")
    preparation_details = {
        "seconds": _seconds(preparation.get("seconds")),
        "copy_verification_seconds": _seconds(scenario.get("copy_verification_seconds")),
        "cache_transfer_method": scenario.get("cache_transfer_method"),
        "copied_bytes": scenario.get("copied_bytes"),
        "linked_bytes": scenario.get("linked_bytes"),
        "preparation_cache_released_after_verification": scenario.get("preparation_cache_released_after_verification"),
        "resources": _resources(preparation["resources"]) if "resources" in preparation else None,
    }
    total = _seconds(summary.get("total_seconds"))
    inputs = [{"profile": profile, "training_input_hash": input_hash} for profile, input_hash in sorted({
        (str(head["profile"]), str(head["training_input_hash"])) for head in heads
    })]
    identity: dict[str, object] = {
        "schema_version": _IDENTITY_SCHEMA,
        "training_configuration_sha256": configuration,
        "inputs": inputs,
        "numeric_fingerprints": fingerprints,
        "initial_history_rows_read": rows,
    }
    numeric_failure = (
        "training_benchmark_repeat_numeric_values_changed" if repeat_numeric else
        "training_benchmark_sample_reuse_model_values_mismatch" if sample_numeric else None
    )
    if status == "degraded" and numeric_failure is None:
        raise ValueError("training_benchmark_child_not_passed")
    return _ChildReview(identity, {
        "benchmark_mode": mode,
        "training_configuration_sha256": configuration,
        "training_implementation_sha256": implementation,
        "input_mode": input_mode,
        "published_source_guard": guard,
        "heads": heads,
        "numeric_fingerprints": fingerprints,
        "workloads": workloads,
        "preparation": preparation_details,
        "policies": policies,
        "total_seconds": total,
    }, total, mode, numeric_failure=numeric_failure)


def _failure_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(code, str) for code in value):
        raise ValueError("training_benchmark_evidence_invalid:failure_codes_invalid")
    return tuple(value)


def _head_values(run: dict[str, object], expected: tuple[str, ...], *, reused: bool) -> list[dict[str, object]]:
    if (run.get("complete") is not True or type(run.get("exit_code")) is not int or run.get("exit_code") != 0
            or type(run.get("reloaded_heads")) is not int or run.get("reloaded_heads") != len(expected) * 2):
        raise ValueError("training_benchmark_run_incomplete")
    profiles = run.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != len(expected):
        raise ValueError("training_benchmark_profiles_incomplete")
    values: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in profiles:
        profile = _mapping(value, "training_benchmark_profile_invalid")
        name = _text(profile.get("profile"), "training_benchmark_profile_invalid")
        if name not in expected or name in seen or profile.get("status") != "engineering_ready" or profile.get("reused") is not reused:
            raise ValueError("training_benchmark_profile_invalid")
        seen.add(name)
        input_hash = _hash(profile.get("training_input_hash"))
        heads = profile.get("heads")
        if not isinstance(heads, list) or len(heads) != 2:
            raise ValueError("training_benchmark_heads_incomplete")
        strategies: set[str] = set()
        for value in heads:
            head = _mapping(value, "training_benchmark_head_invalid")
            strategy = _text(head.get("strategy"), "training_benchmark_head_invalid")
            if strategy not in _STRATEGIES or strategy in strategies or head.get("status") != "engineering_ready":
                raise ValueError("training_benchmark_head_invalid")
            strategies.add(strategy)
            values.append({
                "profile": name, "training_input_hash": input_hash, "strategy": strategy,
                "model_hash": _hash(head.get("model_hash")), "report_hash": _hash(head.get("report_hash")),
            })
    return sorted(values, key=lambda item: (str(item["profile"]), str(item["strategy"])))


def _scan(run: dict[str, object], expected: tuple[str, ...], *, first: bool) -> int:
    execution = _mapping(run.get("execution"), "training_benchmark_execution_missing")
    if execution.get("status") != "completed" or execution.get("profiles") != list(expected):
        raise ValueError("training_benchmark_execution_invalid")
    scans, rows = execution.get("history_scan_passes"), execution.get("history_rows_read")
    if type(scans) is not int or scans != int(first) or type(rows) is not int or (rows <= 0 if first else rows != 0):
        raise ValueError("training_benchmark_history_scan_invalid")
    return rows


def _fingerprints(run: dict[str, object], expected: tuple[str, ...]) -> list[dict[str, object]]:
    values = run.get("numeric_fingerprints")
    if not isinstance(values, list) or len(values) != len(expected) * 2:
        raise ValueError("training_benchmark_numeric_fingerprints_missing")
    parsed: dict[tuple[str, str], str] = {}
    for value in values:
        item = _mapping(value, "training_benchmark_numeric_fingerprint_invalid")
        profile = _text(item.get("profile"), "training_benchmark_numeric_fingerprint_invalid")
        strategy = _text(item.get("strategy"), "training_benchmark_numeric_fingerprint_invalid")
        if profile not in expected or strategy not in _STRATEGIES or (profile, strategy) in parsed:
            raise ValueError("training_benchmark_numeric_fingerprint_invalid")
        parsed[(profile, strategy)] = _hash(item.get("model_values_and_metrics_sha256"))
    return [{"profile": profile, "strategy": strategy, "model_values_and_metrics_sha256": digest}
            for (profile, strategy), digest in sorted(parsed.items())]


def _cache_signatures(value: object) -> list[list[object]]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("training_benchmark_sample_source_signatures_missing")
    parsed: list[list[object]] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("training_benchmark_sample_source_signature_invalid")
        profile = _text(row[0], "training_benchmark_sample_source_signature_invalid")
        if profile not in _PROFILES[1:] or profile in seen or any(type(row[position]) is not int or row[position] <= 0 for position in (2, 3)):
            raise ValueError("training_benchmark_sample_source_signature_invalid")
        seen.add(profile)
        parsed.append([profile, _hash(row[1]), row[2], row[3], _hash(row[4])])
    return sorted(parsed, key=lambda row: str(row[0]))


def _resources(value: object) -> dict[str, object]:
    fields = _mapping(value, "training_benchmark_resources_missing")
    _seconds(fields.get("cpu_seconds"))
    for name in ("observed_peak_rss_bytes", "observed_peak_os_threads", "samples"):
        number = fields.get(name)
        if type(number) is not int or number <= 0:
            raise ValueError("training_benchmark_resources_invalid:" + name)
    for name in ("physical_read_bytes", "physical_write_bytes", "filesystem_free_bytes_at_start", "filesystem_minimum_free_bytes"):
        number = fields.get(name)
        if type(number) is not int or number < 0:
            raise ValueError("training_benchmark_resources_invalid:" + name)
    if type(fields.get("sampling_failures")) is not int or fields.get("sampling_failures") != 0:
        raise ValueError("training_benchmark_resource_sampling_failed")
    for name in ("scope", "filesystem_measurement_scope"):
        if name in fields:
            _text(fields[name], "training_benchmark_resource_scope_invalid")
    if "sampling_interval_seconds" in fields:
        _seconds(fields["sampling_interval_seconds"])
    if "native_thread_utilization_verified" in fields and type(fields["native_thread_utilization_verified"]) is not bool:
        raise ValueError("training_benchmark_resource_scope_invalid")
    for name in ("sampled_peak_swap_bytes", "major_page_faults", "minor_page_faults", "system_swap_in_pages", "system_swap_out_pages"):
        number = fields.get(name)
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError("training_benchmark_resources_invalid:" + name)
    for name, expected in (
        ("system_swap_scope", "whole_machine; /proc/vmstat; page_count_deltas; includes_other_processes"),
        ("page_fault_scope", "current_process; RUSAGE_SELF; excludes_child_processes"),
    ):
        if name in fields and fields[name] != expected:
            raise ValueError("training_benchmark_resource_scope_invalid:" + name)
    return {name: fields[name] for name in _RESOURCE_FIELDS if name in fields}


def _policy(value: object, name: str) -> dict[str, object]:
    fields = _mapping(value, "training_benchmark_policy_missing:" + name)
    if name == "execution_candidate":
        if (fields.get("execution") not in ("baseline", "resources", "ram_matrices", "shared_validation", "normalized_tcn", "combined", "selected")
                or fields.get("cpu") not in ("baseline", "two_plus_one", "three_single")
                or fields.get("fanout", "baseline") not in ("baseline", "batch_double", "queue_double")):
            raise ValueError("training_benchmark_policy_invalid")
        from scripts.training.resources.execution_selection import ParseExecutionComponents

        components = fields.get("components", [])
        if not isinstance(components, list) or any(not isinstance(item, str) for item in components):
            raise ValueError("training_benchmark_policy_components_invalid")
        if fields.get("execution") == "selected":
            if list(ParseExecutionComponents(",".join(components))) != components:
                raise ValueError("training_benchmark_policy_components_invalid")
        elif components:
            raise ValueError("training_benchmark_policy_components_invalid")
        if not isinstance(fields.get("runs"), list) or not fields["runs"]:
            raise ValueError("training_benchmark_policy_runs_invalid")
        if fields.get("execution") != "baseline" or fields.get("cpu") != "baseline" or fields.get("fanout", "baseline") != "baseline":
            _hash(fields.get("candidate_source_hash"))
            if "options" not in fields:
                raise ValueError("training_benchmark_policy_options_invalid")
    elif name == "computation_candidate" and fields.get("name") not in ("baseline", "indexed_anchors", "schema_cache", "combined"):
        raise ValueError("training_benchmark_policy_invalid")
    elif name == "scheduling_candidate" and fields.get("name") not in ("baseline", "fixed", "long_task_first", "borrowing"):
        raise ValueError("training_benchmark_policy_invalid")
    projected: dict[str, object] = {}
    for key in ("name", "execution", "cpu", "fanout", "candidate_source_hash"):
        if key in fields:
            projected[key] = _hash(fields[key]) if key == "candidate_source_hash" else _text(fields[key], "training_benchmark_policy_invalid")
    if "components" in fields:
        projected["components"] = fields["components"]
    if "options" in fields:
        options = _mapping(fields["options"], "training_benchmark_policy_options_invalid")
        if any(type(options.get(flag)) is not bool for flag in _EXECUTION_FLAGS):
            raise ValueError("training_benchmark_policy_options_invalid")
        components = fields.get("components", []) if fields.get("execution") == "selected" else (
            ["resources", "ram_matrices", "shared_validation", "normalized_tcn"]
            if fields.get("execution") == "combined" else [fields.get("execution")]
        )
        if not isinstance(components, list) or options != {
            "memory_first": "resources" in components,
            "release_cpu_before_publish": "resources" in components,
            "shared_matrix_memory": "ram_matrices" in components,
            "shared_validation": "shared_validation" in components,
            "normalized_tcn_batches": "normalized_tcn" in components,
        }:
            raise ValueError("training_benchmark_policy_options_invalid")
        projected["options"] = {flag: options[flag] for flag in _EXECUTION_FLAGS}
    if "runs" in fields:
        runs = fields["runs"]
        if not isinstance(runs, list):
            raise ValueError("training_benchmark_policy_runs_invalid")
        projected["runs"] = [_policy_run(run) for run in runs]
    return projected


def _policy_run(value: object) -> dict[str, object]:
    run = _mapping(value, "training_benchmark_policy_runs_invalid")
    profiles = run.get("profiles")
    if not isinstance(profiles, list) or profiles not in (list(_PROFILES), list(_PROFILES[1:])):
        raise ValueError("training_benchmark_policy_runs_invalid")
    for key in ("cpu_capacity", "native_threads"):
        number = run.get(key)
        if type(number) is not int or number < 1:
            raise ValueError("training_benchmark_policy_runs_invalid")
    for key in ("batch_rows", "queue_batches"):
        if not isinstance(number := run.get(key), int) or isinstance(number, bool) or number < 1:
            raise ValueError("training_benchmark_policy_runs_invalid")
    if "memory_capacity_bytes" not in run:
        raise ValueError("training_benchmark_policy_runs_invalid")
    threads = _mapping(run.get("profile_threads"), "training_benchmark_policy_runs_invalid")
    for profile in _PROFILES[:3]:
        number = threads.get(profile)
        if type(number) is not int or number < 1:
            raise ValueError("training_benchmark_policy_runs_invalid")
    memory = run.get("memory_capacity_bytes")
    if memory is not None and (type(memory) is not int or memory < 1):
        raise ValueError("training_benchmark_policy_runs_invalid")
    return {
        "profiles": profiles, "cpu_capacity": run["cpu_capacity"], "native_threads": run["native_threads"],
        "profile_threads": {profile: threads[profile] for profile in _PROFILES[:3]}, "memory_capacity_bytes": memory,
        "batch_rows": run["batch_rows"], "queue_batches": run["queue_batches"],
    }


def _mapping(value: object, reason: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(reason)
    return {key: item for key, item in value.items()}


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(reason)
    return value


def _hash(value: object) -> str:
    digest = _text(value, "training_benchmark_hash_invalid")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("training_benchmark_hash_invalid")
    return digest


def _seconds(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("training_benchmark_duration_invalid")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("training_benchmark_duration_invalid") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("training_benchmark_duration_invalid")
    return number


def _read_document(path: Path) -> tuple[dict[str, object], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("training_benchmark_record_not_regular")
    with path.open("rb") as stream:
        contents = stream.read(_REPORT_LIMIT + 1)
    if len(contents) > _REPORT_LIMIT:
        raise ValueError("training_benchmark_record_too_large")
    try:
        value: object = json.loads(contents, object_pairs_hook=_json_pairs, parse_constant=_nonfinite, parse_float=_finite_float)
    except RecursionError as exc:
        raise ValueError("training_benchmark_json_too_deep") from exc
    return _mapping(value, "training_benchmark_record_not_object"), sha256(contents).hexdigest()


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("training_benchmark_json_duplicate_key")
        values[key] = value
    return values


def _nonfinite(value: str) -> object:
    raise ValueError("training_benchmark_json_nonfinite")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("training_benchmark_json_nonfinite")
    return number


def _render(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: dict[str, object]) -> str:
    return sha256(_render(value).encode("utf-8")).hexdigest()


def _require_identity(
    persisted: dict[str, object], current: dict[str, object], *, compare_numeric: bool = True,
) -> None:
    if set(persisted) != set(current) or persisted.get("schema_version") != _IDENTITY_SCHEMA:
        raise ValueError("training_benchmark_frozen_identity_invalid")
    if persisted["training_configuration_sha256"] != current["training_configuration_sha256"]:
        raise ValueError("training_benchmark_configuration_identity_changed")
    if persisted["inputs"] != current["inputs"]:
        raise ValueError("training_benchmark_input_identity_changed")
    if persisted["initial_history_rows_read"] != current["initial_history_rows_read"]:
        raise ValueError("training_benchmark_source_population_changed")
    if not compare_numeric and _fingerprints(
        {"numeric_fingerprints": persisted["numeric_fingerprints"]}, _PROFILES
    ) != persisted["numeric_fingerprints"]:
        raise ValueError("training_benchmark_frozen_identity_invalid")
    if compare_numeric and persisted["numeric_fingerprints"] != current["numeric_fingerprints"]:
        before = persisted["numeric_fingerprints"]
        after = current["numeric_fingerprints"]
        if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
            raise ValueError("training_benchmark_frozen_identity_invalid")
        for original, changed in zip(before, after, strict=True):
            if original != changed:
                head = _mapping(changed, "training_benchmark_numeric_fingerprint_invalid")
                raise ValueError("training_benchmark_numeric_values_changed:" + str(head["profile"]) + ":" + str(head["strategy"]))


def _failure_reason(exc: OSError | UnicodeError | ValueError) -> str:
    if isinstance(exc, ValueError) and str(exc).startswith("training_benchmark_"):
        return str(exc)
    return "training_benchmark_record_" + type(exc).__name__


def _failed(schema: str, reason: str) -> dict[str, object]:
    codes = reason.removeprefix("training_benchmark_child_review_failed:").removeprefix(
        "training_benchmark_evidence_invalid:"
    ).split(",")
    if any(code.startswith("training_benchmark_configuration_") for code in codes):
        kind = "configuration"
    elif any(code.startswith(("training_benchmark_input_", "sample_reuse_input_")) for code in codes):
        kind = "input"
    elif any(code.startswith((
        "training_benchmark_source_", "training_benchmark_sample_source_", "sample_reuse_source_",
    )) for code in codes):
        kind = "source"
    elif "identity" in reason:
        kind = "identity"
    elif all(_numeric_difference_code(code) for code in codes):
        kind = "numeric"
    else:
        kind = "evidence"
    return {
        "schema_version": schema, "status": "failed", "failure_reasons": [reason],
        "failure_kind": kind,
        "performance_acceptance": False, "production_authority": False,
    }


def _numeric_difference_code(code: str) -> bool:
    if code in {
        "training_benchmark_repeat_numeric_values_changed", "training_benchmark_sample_reuse_model_values_mismatch",
    }:
        return True
    parts = code.split(":")
    return (
        len(parts) == 3 and parts[0] == "training_benchmark_numeric_values_changed"
        and parts[1] in _PROFILES and parts[2] in _STRATEGIES
    )


__all__ = [
    "ReviewTrainingBenchmarkChild",
    "SummarizeTrainingBenchmarkFiles",
]

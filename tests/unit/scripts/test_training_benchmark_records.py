"""Durable child evidence cannot be accepted after tampering or incomplete training."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest

from scripts.training.jobs import benchmark_records
from scripts.training.jobs.benchmark_records import ReviewTrainingBenchmarkChild, SummarizeTrainingBenchmarkFiles
from scripts.training.jobs.training_performance import TrainingReuseFailures, TrainingSampleReuseFailures
from tests.unit.scripts.test_training_execution_performance import _result


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _resources() -> dict[str, object]:
    return {
        "cpu_seconds": 0.5,
        "observed_peak_rss_bytes": 1024**3,
        "observed_peak_os_threads": 4,
        "physical_read_bytes": 0,
        "physical_write_bytes": 4096,
        "samples": 10,
        "sampling_failures": 0,
        "sampling_interval_seconds": 0.25,
        "filesystem_free_bytes_at_start": 60 * 1024**3,
        "filesystem_minimum_free_bytes": 50 * 1024**3,
        "unexpected_payload": "not-for-review-output",
    }


def _payload(number: int = 1, mode: str = "candidate") -> dict[str, object]:
    source = _result(number, mode, (0.8, 1.0, 1.0))
    payload = deepcopy(_mapping(source.payload))
    payload.update(schema_version="training-alignment-diagnostic", benchmark_mode=mode)
    summary = _mapping(payload["summary"])
    summary.update(
        input_mode="published_history_read_only",
        published_source_guard="sqlite_mode_ro; due_projection_persist_false; fitter_unmodified",
        total_seconds=160.0,
        unexpected_payload="not-for-review-output",
    )
    for name in ("initial", "repeat"):
        _mapping(summary[name]).update(exit_code=0, reloaded_heads=8, resources=_resources())
    scenario = _mapping(summary["sample_reuse"])
    _mapping(scenario["run"]).update(exit_code=0, reloaded_heads=6, resources=_resources())
    scenario.update(
        cache_transfer_method="hardlink", linked_bytes=4096, copied_bytes=0,
        preparation_cache_released_after_verification=True,
    )
    signatures = [[profile, "a" * 64, 4096, 1000, "b" * 64] for profile in ("v2", "v3", "v5")]
    scenario["source_cache_signatures_before"] = signatures
    scenario["source_cache_signatures_after"] = deepcopy(signatures)
    _mapping(scenario["preparation"])["resources"] = _resources()
    return payload


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def _review(tmp_path: Path, payload: dict[str, object], stem: str = "child") -> tuple[Path, dict[str, object]]:
    raw = tmp_path / f"{stem}.json"
    _write(raw, payload)
    reviewed = ReviewTrainingBenchmarkChild(raw, identity_path=tmp_path / "identity.json")
    record = tmp_path / f"{stem}.review.json"
    _write(record, reviewed)
    return record, reviewed


def _refresh_failures(payload: dict[str, object]) -> None:
    summary = _mapping(payload["summary"])
    scenario = _mapping(summary["sample_reuse"])
    profiles = ("v1", "v2", "v3", "v5")
    summary["repeat_reuse_failures"] = list(TrainingReuseFailures(summary["initial"], summary["repeat"], profiles))
    scenario["failures"] = list(TrainingSampleReuseFailures(summary["initial"], scenario, profiles))


def _degraded_numeric_payload(kind: str) -> dict[str, object]:
    payload = _payload()
    payload["status"] = "degraded"
    summary = _mapping(payload["summary"])
    scenario = _mapping(summary["sample_reuse"])
    run = _mapping(summary["repeat"] if kind == "repeat" else scenario["run"])
    _mapping(run["numeric_fingerprints"][0])["model_values_and_metrics_sha256"] = "f" * 64
    summary["repeat_model_values_equal"] = kind != "repeat"
    if kind == "sample":
        scenario["preparation_cache_released_after_verification"] = False
    _refresh_failures(payload)
    return payload


@pytest.mark.parametrize("kind", ("repeat", "sample"))
@pytest.mark.parametrize("frozen", (False, True))
def test_degraded_pure_numeric_difference_is_rejected_without_sealing_or_changing_identity(
    tmp_path: Path, kind: str, frozen: bool,
) -> None:
    identity = tmp_path / "identity.json"
    if frozen:
        _review(tmp_path, _payload(mode="baseline_before"), "baseline")
    before = identity.read_bytes() if frozen else None
    payload = _degraded_numeric_payload(kind)
    if kind == "sample":
        assert _mapping(_mapping(payload["summary"])["sample_reuse"])["preparation_cache_released_after_verification"] is False
    _record, rejected = _review(tmp_path, payload, "degraded")
    assert rejected["status"] == "failed" and rejected["failure_kind"] == "numeric"
    assert rejected["failure_reasons"] == [
        "training_benchmark_repeat_numeric_values_changed" if kind == "repeat"
        else "training_benchmark_sample_reuse_model_values_mismatch"
    ]
    assert "verified" not in rejected
    assert (identity.read_bytes() if identity.exists() else None) == before


@pytest.mark.parametrize("kind", ("repeat", "sample"))
@pytest.mark.parametrize(("mutation", "failure_kind"), (
    ("source", "source"), ("resources", "evidence"), ("budget", "evidence"),
    ("configuration", "configuration"), ("input", "input"),
))
def test_non_numeric_evidence_precedes_an_actual_numeric_difference(
    tmp_path: Path, kind: str, mutation: str, failure_kind: str,
) -> None:
    _review(tmp_path, _payload(mode="baseline_before"), "baseline")
    identity = tmp_path / "identity.json"
    before = identity.read_bytes()
    payload = _degraded_numeric_payload(kind)
    summary = _mapping(payload["summary"])
    if mutation == "source":
        summary["source_files_unchanged"] = False
    elif mutation == "resources":
        del _mapping(_mapping(summary["repeat"])["resources"])["filesystem_minimum_free_bytes"]
    elif mutation == "budget":
        _mapping(summary["execution_candidate"])["runs"] = []
    elif mutation == "configuration":
        summary["training_configuration_sha256"] = "f" * 64
    else:
        for name in ("initial", "repeat"):
            _mapping(_mapping(summary[name])["profiles"][0])["training_input_hash"] = "f" * 64
        _refresh_failures(payload)
    _record, rejected = _review(tmp_path, payload, "mixed")
    assert rejected["status"] == "failed" and rejected["failure_kind"] == failure_kind
    assert "verified" not in rejected and identity.read_bytes() == before


@pytest.mark.parametrize("kind", ("repeat", "sample"))
def test_failed_status_cannot_borrow_a_numeric_difference_to_continue(tmp_path: Path, kind: str) -> None:
    payload = _degraded_numeric_payload(kind)
    payload["status"] = "failed"
    _record, rejected = _review(tmp_path, payload)
    assert rejected["failure_kind"] == "evidence"
    assert rejected["failure_reasons"] == ["training_benchmark_child_not_passed"]
    assert not (tmp_path / "identity.json").exists()


@pytest.mark.parametrize("claimed", (False, True))
def test_degraded_status_requires_a_real_numeric_difference(tmp_path: Path, claimed: bool) -> None:
    payload = _payload()
    payload["status"] = "degraded"
    if claimed:
        _mapping(_mapping(payload["summary"])["sample_reuse"])["failures"] = ["sample_reuse_model_values_mismatch"]
    _record, rejected = _review(tmp_path, payload)
    assert rejected["failure_kind"] == "evidence" and "verified" not in rejected
    assert not (tmp_path / "identity.json").exists()


@pytest.mark.parametrize("mutation", ("missing", "not_list", "hash", "duplicate", "profile"))
def test_malformed_numeric_fingerprints_are_evidence_errors(tmp_path: Path, mutation: str) -> None:
    payload = _degraded_numeric_payload("repeat")
    repeat = _mapping(_mapping(payload["summary"])["repeat"])
    if mutation == "missing":
        del repeat["numeric_fingerprints"]
    elif mutation == "not_list":
        repeat["numeric_fingerprints"] = "missing"
    elif mutation == "hash":
        _mapping(repeat["numeric_fingerprints"][0])["model_values_and_metrics_sha256"] = "bad"
    elif mutation == "duplicate":
        repeat["numeric_fingerprints"][1] = repeat["numeric_fingerprints"][0]
    else:
        _mapping(repeat["numeric_fingerprints"][0])["profile"] = "unknown"
    _record, rejected = _review(tmp_path, payload)
    assert rejected["failure_kind"] == "evidence" and "verified" not in rejected
    assert not (tmp_path / "identity.json").exists()


@pytest.mark.parametrize("kind", ("repeat", "sample"))
def test_aggregation_rejects_a_forged_verified_record_with_a_numeric_failure(tmp_path: Path, kind: str) -> None:
    entries = _entries(tmp_path, 1)
    _name, path = entries[1]
    record = json.loads(path.read_text())
    raw = Path(record["report_path"])
    _write(raw, _degraded_numeric_payload(kind))
    record["report_sha256"] = sha256(raw.read_bytes()).hexdigest()
    _write(path, record)
    identity = tmp_path / "identity.json"
    before = identity.read_bytes()
    rejected = SummarizeTrainingBenchmarkFiles(entries)
    assert rejected["status"] == "failed" and rejected["failure_kind"] == "numeric"
    assert identity.read_bytes() == before


def test_first_valid_child_seals_identity_and_returns_only_named_evidence(tmp_path: Path) -> None:
    record, reviewed = _review(tmp_path, _payload())
    assert reviewed["status"] == "verified" and reviewed["verified"] is True
    identity = json.loads((tmp_path / "identity.json").read_text())
    assert identity == reviewed["identity"]
    details = _mapping(reviewed["details"])
    workloads = _mapping(details["workloads"])
    assert set(workloads) == {"initial", "repeat", "sample_reuse"}
    assert _mapping(workloads["initial"])["seconds"] == 80.0
    assert _mapping(workloads["repeat"])["history_scan_passes"] == 0
    assert _mapping(workloads["sample_reuse"])["history_rows_read"] == 0
    assert "not-for-review-output" not in record.read_text()
    assert reviewed["performance_acceptance"] is False and reviewed["production_authority"] is False


def test_selected_components_and_actual_fanout_survive_review(tmp_path: Path) -> None:
    payload = _payload()
    policy = _mapping(_mapping(payload["summary"])["execution_candidate"])
    policy.update(execution="selected", components=["resources", "normalized_tcn"], fanout="batch_double")
    policy["options"] = {
        "memory_first": True, "release_cpu_before_publish": True, "shared_matrix_memory": False,
        "shared_validation": False, "normalized_tcn_batches": True,
    }
    policy["runs"] = [{
        "profiles": ["v1", "v2", "v3", "v5"], "cpu_capacity": 3, "native_threads": 1,
        "profile_threads": {"v1": 2, "v2": 2, "v3": 2}, "memory_capacity_bytes": 1024,
        "batch_rows": 512, "queue_batches": 2,
    }]
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "verified"
    projected = _mapping(_mapping(_mapping(reviewed["details"])["policies"])["execution_candidate"])
    assert projected["components"] == ["resources", "normalized_tcn"]
    assert projected["fanout"] == "batch_double"
    assert projected["runs"] == policy["runs"]


@pytest.mark.parametrize("components", ([], ["resources", "resources"], ["normalized_tcn", "resources"], ["unknown"]))
def test_invalid_selected_components_cannot_seal_identity(tmp_path: Path, components: list[str]) -> None:
    payload = _payload()
    _mapping(_mapping(payload["summary"])["execution_candidate"]).update(execution="selected", components=components)
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "failed"
    assert not (tmp_path / "identity.json").exists()


def test_baseline_cannot_hide_fanout_override(tmp_path: Path) -> None:
    payload = _payload(mode="baseline_before")
    policy = _mapping(_mapping(payload["summary"])["execution_candidate"])
    policy.update(fanout="queue_double", candidate_source_hash="a" * 64)
    policy["options"] = {flag: False for flag in benchmark_records._EXECUTION_FLAGS}
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "failed"
    assert not (tmp_path / "identity.json").exists()


def test_selected_policy_cannot_hide_unselected_enabled_options(tmp_path: Path) -> None:
    payload = _payload()
    _mapping(_mapping(payload["summary"])["execution_candidate"]).update(
        execution="selected", components=["resources"],
    )
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "failed"
    assert not (tmp_path / "identity.json").exists()


@pytest.mark.parametrize("number", (None, 0, 123))
def test_swap_and_fault_evidence_preserves_missing_or_measured_values(tmp_path: Path, number: int | None) -> None:
    payload = _payload()
    resources = _mapping(_mapping(_mapping(payload["summary"])["initial"])["resources"])
    resources.update(
        sampled_peak_swap_bytes=number, major_page_faults=number, minor_page_faults=number,
        system_swap_in_pages=number, system_swap_out_pages=number,
        system_swap_scope="whole_machine; /proc/vmstat; page_count_deltas; includes_other_processes",
        page_fault_scope="current_process; RUSAGE_SELF; excludes_child_processes",
    )
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "verified"
    projected = _mapping(_mapping(_mapping(_mapping(reviewed["details"])["workloads"])["initial"])["resources"])
    assert projected["sampled_peak_swap_bytes"] == number
    assert projected["major_page_faults"] == number and projected["system_swap_in_pages"] == number


@pytest.mark.parametrize("number", (-1, True, 1.5))
def test_invalid_swap_evidence_is_not_a_numeric_candidate_rejection(tmp_path: Path, number: object) -> None:
    payload = _payload()
    _mapping(_mapping(_mapping(payload["summary"])["initial"])["resources"])["sampled_peak_swap_bytes"] = number
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "failed" and reviewed["failure_kind"] == "evidence"
    assert not (tmp_path / "identity.json").exists()


def test_external_history_and_copied_samples_keep_the_original_evidence_contract(tmp_path: Path) -> None:
    payload = _payload()
    summary = _mapping(payload["summary"])
    summary.update(input_mode="external_history_copy", published_source_guard="not_required_for_isolated_copy")
    _mapping(summary["sample_reuse"]).update(
        cache_transfer_method="copy", copied_bytes=4096, linked_bytes=0,
        preparation_cache_released_after_verification=False,
    )
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "verified"


@pytest.mark.parametrize("mutation", (
    "not_passed", "source_writes_bool", "source_files", "source_identity", "guard", "initial_scan",
    "initial_rows", "initial_reused", "missing_head", "repeat_scan", "repeat_artifact", "repeat_numeric",
    "sample_scan", "sample_reused", "sample_numeric", "sample_signature", "hardlink_retained", "resources",
    "sampling_failed", "baseline_candidate",
))
def test_incomplete_or_changed_evidence_never_seals_an_identity(tmp_path: Path, mutation: str) -> None:
    payload = _payload(mode="baseline_before" if mutation == "baseline_candidate" else "candidate")
    summary = _mapping(payload["summary"])
    initial, repeat = _mapping(summary["initial"]), _mapping(summary["repeat"])
    scenario = _mapping(summary["sample_reuse"])
    sample = _mapping(scenario["run"])
    if mutation == "not_passed":
        payload["status"] = "degraded"
    elif mutation == "source_writes_bool":
        summary["source_data_writes"] = False
    elif mutation in {"source_files", "source_identity"}:
        summary["source_files_unchanged" if mutation == "source_files" else "source_identity_unchanged"] = False
    elif mutation == "guard":
        del summary["published_source_guard"]
    elif mutation in {"initial_scan", "repeat_scan", "sample_scan"}:
        run = initial if mutation == "initial_scan" else repeat if mutation == "repeat_scan" else sample
        _mapping(run["execution"])["history_scan_passes"] = 2
    elif mutation == "initial_rows":
        _mapping(initial["execution"])["history_rows_read"] = 0
    elif mutation in {"initial_reused", "sample_reused"}:
        _mapping((initial if mutation == "initial_reused" else sample)["profiles"][0])["reused"] = True
    elif mutation == "missing_head":
        _mapping(initial["profiles"][0])["heads"].pop()
    elif mutation == "repeat_artifact":
        _mapping(_mapping(repeat["profiles"][0])["heads"][0])["model_hash"] = "f" * 64
    elif mutation in {"repeat_numeric", "sample_numeric"}:
        _mapping((repeat if mutation == "repeat_numeric" else sample)["numeric_fingerprints"][0])["model_values_and_metrics_sha256"] = "f" * 64
    elif mutation == "sample_signature":
        scenario["source_cache_signatures_after"][0][4] = "f" * 64
    elif mutation == "hardlink_retained":
        scenario["preparation_cache_released_after_verification"] = False
    elif mutation == "resources":
        del _mapping(initial["resources"])["filesystem_minimum_free_bytes"]
    elif mutation == "sampling_failed":
        _mapping(repeat["resources"])["sampling_failures"] = 1
    else:
        _mapping(summary["execution_candidate"])["cpu"] = "three_single"
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "failed" and "verified" not in reviewed
    assert reviewed["failure_reasons"]
    assert not (tmp_path / "identity.json").exists()


@pytest.mark.parametrize("mutation", ("configuration", "input", "numeric", "source"))
def test_later_identity_drift_is_rejected_without_overwriting_the_first_child(tmp_path: Path, mutation: str) -> None:
    _review(tmp_path, _payload(), "first")
    before = (tmp_path / "identity.json").read_bytes()
    payload = _payload(mode="baseline_after")
    summary = _mapping(payload["summary"])
    if mutation == "configuration":
        summary["training_configuration_sha256"] = "f" * 64
    elif mutation == "source":
        _mapping(_mapping(summary["initial"])["execution"])["history_rows_read"] = 401
    else:
        for name in ("initial", "repeat"):
            run = _mapping(summary[name])
            if mutation == "input":
                _mapping(run["profiles"][0])["training_input_hash"] = "f" * 64
            else:
                _mapping(run["numeric_fingerprints"][0])["model_values_and_metrics_sha256"] = "f" * 64
    _record, reviewed = _review(tmp_path, payload, "changed")
    assert reviewed["status"] == "failed"
    reason = {
        "configuration": "training_benchmark_configuration_identity_changed",
        "input": "training_benchmark_input_identity_changed",
        "numeric": "training_benchmark_numeric_values_changed:v1:tomorrow",
        "source": "training_benchmark_source_population_changed",
    }[mutation]
    assert reviewed["failure_reasons"] == [reason]
    assert reviewed["failure_kind"] == mutation
    assert (tmp_path / "identity.json").read_bytes() == before


def test_first_writer_wins_when_two_valid_inputs_are_reviewed_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _payload(), _payload()
    _mapping(second["summary"])["training_configuration_sha256"] = "f" * 64
    paths = (tmp_path / "one.json", tmp_path / "two.json")
    for path, payload in zip(paths, (first, second), strict=True):
        _write(path, payload)
    barrier = Barrier(2)
    publish = benchmark_records.PublishImmutable

    def simultaneous(path: Path, rendered: str) -> bool:
        barrier.wait(timeout=5)
        return publish(path, rendered)

    monkeypatch.setattr(benchmark_records, "PublishImmutable", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reviews = tuple(workers.map(lambda path: ReviewTrainingBenchmarkChild(path, identity_path=tmp_path / "identity.json"), paths))
    assert sorted(str(review["status"]) for review in reviews) == ["failed", "verified"]
    winner = next(review for review in reviews if review["status"] == "verified")
    assert json.loads((tmp_path / "identity.json").read_text()) == winner["identity"]
    assert not tuple(tmp_path.glob(".identity.json.*"))


def test_publication_io_failure_cannot_create_a_verified_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(path: Path, rendered: str) -> bool:
        raise OSError("fixture unavailable")

    monkeypatch.setattr(benchmark_records, "PublishImmutable", unavailable)
    _record, reviewed = _review(tmp_path, _payload())
    assert reviewed["status"] == "failed" and "verified" not in reviewed
    assert not (tmp_path / "identity.json").exists()


@pytest.mark.parametrize("raw", (
    '{"status":"passed","status":"failed"}', '{"number":NaN}', '{"number":1e400}', '{"broken"',
    '[' * 2000 + '0' + ']' * 2000,
))
def test_invalid_json_cannot_change_an_existing_identity(tmp_path: Path, raw: str) -> None:
    _review(tmp_path, _payload(), "valid")
    before = (tmp_path / "identity.json").read_bytes()
    report = tmp_path / "invalid.json"
    report.write_text(raw)
    reviewed = ReviewTrainingBenchmarkChild(report, identity_path=tmp_path / "identity.json")
    assert reviewed["status"] == "failed" and "verified" not in reviewed
    assert (tmp_path / "identity.json").read_bytes() == before


def _entries(tmp_path: Path, rounds: int) -> tuple[tuple[str, Path], ...]:
    entries = []
    for number in range(1, rounds + 1):
        for mode in ("baseline_before", "candidate", "baseline_after"):
            stem = f"round-{number}-{mode}"
            record, review = _review(tmp_path, _payload(number, mode), stem)
            assert review["status"] == "verified", review
            middle = f"round_{number}:" if rounds > 1 else ""
            entries.append((f"training_alignment:{middle}{mode}:v1,v2,v3,v5", record))
    return tuple(entries)


@pytest.mark.parametrize("rounds", (1, 5))
def test_verified_files_use_the_existing_three_child_or_five_group_summary(tmp_path: Path, rounds: int) -> None:
    comparison = SummarizeTrainingBenchmarkFiles(_entries(tmp_path, rounds))
    assert comparison["status"] == "verified" and comparison["comparable"] is True
    assert comparison["reviewed_children"] == rounds * 3
    if rounds == 5:
        assert comparison["timing_acceptance"] is True
        assert comparison["initial_wins"] == 5
    assert comparison["performance_acceptance"] is False and comparison["production_authority"] is False


@pytest.mark.parametrize("mutation", ("raw_report", "identity", "reviewed_digest", "unverified", "name", "duplicate"))
def test_aggregation_rechecks_durable_records_and_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    entries = _entries(tmp_path, 1)
    name, path = entries[1]
    record = json.loads(path.read_text())
    if mutation == "raw_report":
        raw = Path(record["report_path"])
        payload = json.loads(raw.read_text())
        payload["summary"]["initial"]["seconds"] = 1.0
        _write(raw, payload)
    elif mutation == "identity":
        identity = json.loads((tmp_path / "identity.json").read_text())
        identity["training_configuration_sha256"] = "f" * 64
        _write(tmp_path / "identity.json", identity)
    elif mutation in {"reviewed_digest", "unverified"}:
        if mutation == "reviewed_digest":
            record["report_sha256"] = "f" * 64
        else:
            record["status"] = "failed"
        _write(path, record)
    elif mutation == "name":
        entries = (entries[0], (name.replace("candidate", "baseline_before"), path), entries[2])
    else:
        entries = (*entries, entries[1])
    comparison = SummarizeTrainingBenchmarkFiles(entries)
    assert comparison["status"] == "failed" and "verified" not in comparison
    assert comparison["failure_reasons"]
    assert comparison["performance_acceptance"] is False


def test_symlinked_raw_reports_and_identity_files_are_never_adopted(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    _write(raw, _payload())
    linked = tmp_path / "linked.json"
    linked.symlink_to(raw)
    assert ReviewTrainingBenchmarkChild(linked, identity_path=tmp_path / "identity.json")["status"] == "failed"
    identity = tmp_path / "identity.json"
    identity.symlink_to(raw)
    assert ReviewTrainingBenchmarkChild(raw, identity_path=identity)["status"] == "failed"
    assert json.loads(raw.read_text())["status"] == "passed"


def test_aggregation_preserves_a_failed_candidates_numeric_rejection_reason(tmp_path: Path) -> None:
    entries = _entries(tmp_path, 1)
    changed = _payload()
    for name in ("initial", "repeat"):
        _mapping(_mapping(_mapping(changed["summary"])[name])["numeric_fingerprints"][0])["model_values_and_metrics_sha256"] = "f" * 64
    record, rejected = _review(tmp_path, changed, "numeric-rejected")
    assert rejected["failure_kind"] == "numeric"
    comparison = SummarizeTrainingBenchmarkFiles((entries[0], (entries[1][0], record), entries[2]))
    assert comparison["status"] == "failed" and comparison["failure_kind"] == "numeric"
    assert "training_benchmark_numeric_values_changed:v1:tomorrow" in str(comparison["failure_reasons"])


def test_huge_numeric_durations_are_rejected_without_an_uncaught_overflow(tmp_path: Path) -> None:
    payload = _payload()
    _mapping(_mapping(payload["summary"])["initial"])["seconds"] = 10 ** 1000
    _record, reviewed = _review(tmp_path, payload)
    assert reviewed["status"] == "failed" and "verified" not in reviewed
    assert not (tmp_path / "identity.json").exists()

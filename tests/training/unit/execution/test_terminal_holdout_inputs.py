from __future__ import annotations

import hashlib
import json
import os
import shutil
import errno
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import time
from pathlib import Path
from threading import Barrier

import pytest

from trader.training.application.terminal_holdout_execution import (
    ExecuteTerminalHoldout,
    ResearchCancelledError,
    TerminalHoldoutAlreadyOpenedError,
)
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState, TerminalHoldoutRow
from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeDateSplit
from trader.training.infra.research.command_evidence import TerminalConclusionPublisher
from trader.training.infra.research.command_evidence import TerminalHoldoutParentsReader
from trader.training.infra.research.terminal_holdout_inputs import TerminalHoldoutInputFiles
from trader.training.infra.research_settings import LoadResearchPlan
from trader.training.infra.research.point_in_time_dataset_evidence import EncodePointInTimeDateSplit
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash
from trader.training.infra.memory_admission import TrainingMemoryBudget
from trader.infra.regular_file import RegularFileStream
from tests.helpers.training.research.registrations import (
    CandidateRegistrationFixture, RegistrationPayloadFixture, StateRegistrationFixture,
)

_CONFIG = Path(__file__).resolve().parents[4] / "config"


def _split(fit_dates=750):
    terminal = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(200))
    start = terminal[0] - timedelta(days=fit_dates + 150)

    def dates(first, count):
        return tuple(start + timedelta(days=index) for index in range(first, first + count))

    return PointInTimeDateSplit(
        dates(0, fit_dates), dates(fit_dates, 20), dates(fit_dates + 20, 30), dates(fit_dates + 50, 5),
        dates(fit_dates + 55, 50), dates(fit_dates + 105, 5), terminal,
    )


class _Parents:
    def __init__(self, status="historical_candidate_ready", *, profile_id="v2", fit_dates=750):
        self.values = (
            TerminalHoldoutParentState(
                status, "a" * 64, "b" * 64, date_split=_split(fit_dates) if status == "historical_candidate_ready" else None,
                profile_id=profile_id, registration=CandidateRegistrationFixture(profile_id, fit_dates=fit_dates) if status == "historical_candidate_ready" else None,
            ),
            TerminalHoldoutParentState(
                status, "c" * 64, "d" * 64, date_split=_split(fit_dates) if status == "historical_candidate_ready" else None,
                profile_id=profile_id, registration=CandidateRegistrationFixture(profile_id, strategy="d25", fit_dates=fit_dates) if status == "historical_candidate_ready" else None,
            ),
        )

    def read(self):
        return self.values


def _seal_parents(root, parents):
    root.mkdir()
    payload = {
        strategy: {
            "candidate_status": parent.candidate_status, "parent_hash": parent.parent_hash,
            "candidate_hash": parent.candidate_hash, "failure_reasons": list(parent.failure_reasons),
            "date_split": EncodePointInTimeDateSplit(parent.date_split) if parent.date_split is not None else None,
            "profile_id": parent.profile_id,
            "registration": RegistrationPayloadFixture(parent.registration) if parent.registration is not None else None,
        }
        for strategy, parent in zip(("tomorrow", "d25"), parents.values, strict=True)
    }
    (root / "terminal_parents.json").write_text(
        json.dumps({**payload, "content_hash": CanonicalResearchHash(payload)}), encoding="utf-8"
    )


def _inputs(root, parents, *, config_dir=_CONFIG, vary_state=False):
    root.mkdir()
    plan = LoadResearchPlan(config_dir)
    manifest = {"research_plan_hash": plan.content_hash}
    for strategy, parent in zip(("tomorrow", "d25"), parents.values, strict=True):
        encoded = []
        assert parent.date_split is not None
        dates = parent.date_split.terminal_holdout_dates
        state = StateRegistrationFixture(dates, vary=vary_state)
        for index, day in enumerate(dates):
            market, volatility, liquidity = state.states(state.days[index])
            board, code = (("main", "600001"), ("chinext", "300001"), ("star", "688001"))[index % 3] if vary_state else ("main", "600001")
            row = TerminalHoldoutRow(
                day, code, board, "fixture-industry", market, volatility, liquidity,
                0.02, (0.018, 0.015, 0.01), (0.008, 0.005, 0.0),
                False, False, False, False, -0.1, -0.1, False, False, 10_000, 10_000, True,
                (0.02, 0.02, 0.02, 0.02) if strategy == "d25" else (),
                (0.01, 0.01, 0.01, 0.01) if strategy == "d25" else (),
                ranking_score=50.0, local_score=50.0,
            )
            payload = asdict(row)
            payload["trade_date"] = day.isoformat()
            encoded.append(json.dumps(payload, sort_keys=True) + "\n")
        content = "".join(encoded).encode()
        (root / f"{strategy}.jsonl").write_bytes(content)
        manifest[strategy] = {
            "parent_hash": parent.parent_hash, "candidate_hash": parent.candidate_hash,
            "trade_dates": [value.isoformat() for value in dates],
            "rows_sha256": hashlib.sha256(content).hexdigest(), "row_count": len(dates),
            "profile_id": parent.profile_id,
            "registration_hash": parent.registration.content_hash,
            "state_registration": RegistrationPayloadFixture(state),
        }
    (root / "plan.json").write_text(json.dumps(manifest), encoding="utf-8")
    return TerminalHoldoutInputFiles(root, root / "opened.json"), plan


def _open(data, evidence, strategies):
    with data.prepare(evidence, strategies) as prepared:
        return prepared.open_once()


def _use_case(parents, data, publisher, plan, **options):
    return ExecuteTerminalHoldout(
        parents, data, publisher, plan, options.pop("memory_admission", TrainingMemoryBudget(512 * 1024**2)),
        now=options.pop("now", lambda: datetime(2026, 11, 8, tzinfo=ZoneInfo("Asia/Shanghai"))),
        monotonic=options.pop("monotonic", time.monotonic),
        cancel_requested=options.pop("cancel_requested", lambda: False), **options,
    )


@dataclass
class _OutcomeAccess:
    read_calls: list[Path] = field(default_factory=list)
    descriptors: list[int] = field(default_factory=list)

    def assert_closed(self) -> None:
        assert self.descriptors
        for descriptor in self.descriptors:
            with pytest.raises(OSError) as failure:
                os.fstat(descriptor)
            assert failure.value.errno == errno.EBADF


class _ObservedOutcomeStream:
    def __init__(self, source: RegularFileStream, path: Path, access: _OutcomeAccess) -> None:
        self._source, self._path, self._access = source, path, access

    def read(self, size: int = -1, /) -> bytes:
        self._access.read_calls.append(self._path)
        return self._source.read(size)

    def readline(self, size: int = -1, /) -> bytes:
        self._access.read_calls.append(self._path)
        return self._source.readline(size)

    def seek(self, offset: int, whence: int = 0, /) -> int:
        return self._source.seek(offset, whence)

    def fileno(self) -> int:
        return self._source.fileno()

    def verify(self, path: Path | None = None) -> None:
        self._source.verify(path)


def _observe_outcome_reads(monkeypatch: pytest.MonkeyPatch) -> _OutcomeAccess:
    from trader.training.infra.research import terminal_holdout_inputs

    access = _OutcomeAccess()
    original = terminal_holdout_inputs.OpenRegularFile

    @contextmanager
    def opened(path: Path, *, maximum_bytes: int, check: Callable[[], None] | None = None) -> Iterator[RegularFileStream]:
        with original(path, maximum_bytes=maximum_bytes, check=check) as source:
            access.descriptors.append(source.fileno())
            yield _ObservedOutcomeStream(source, path, access)

    monkeypatch.setattr(terminal_holdout_inputs, "OpenRegularFile", opened)
    return access


def _assert_full_budget_available(budget: TrainingMemoryBudget) -> None:
    attempts = []

    def heartbeat() -> None:
        attempts.append(True)
        assert len(attempts) == 1, "the prior execution retained its reservation"

    with budget.reserve(budget.capacity_bytes, heartbeat):
        assert attempts == [True]


@pytest.mark.parametrize("rejection", ["insufficient", "cancelled", "timeout"])
def test_admission_rejection_reads_no_outcomes_and_preserves_the_unopened_partition(tmp_path, monkeypatch, rejection):
    parents = _Parents()
    data, plan = _inputs(tmp_path / "inputs", parents)
    access = _observe_outcome_reads(monkeypatch)
    budget = TrainingMemoryBudget(1 if rejection == "insufficient" else 512 * 1024**2)
    cancelled_checks = []
    clock = iter((0.0, 0.0, 31.0))

    def cancelled():
        cancelled_checks.append(True)
        return rejection == "cancelled" and len(cancelled_checks) >= 3

    error = {"insufficient": ValueError, "cancelled": ResearchCancelledError, "timeout": TimeoutError}[rejection]
    reason = {
        "insufficient": "training_memory_request_exceeds_budget", "cancelled": "research_cancelled",
        "timeout": "research_memory_admission_timeout",
    }[rejection]
    with ExitStack() as held:
        if rejection != "insufficient":
            held.enter_context(budget.reserve(budget.capacity_bytes, lambda: None))
        with pytest.raises(error, match=reason):
            _use_case(
                parents, data, TerminalConclusionPublisher(tmp_path / "out"), plan,
                memory_admission=budget, cancel_requested=cancelled,
                monotonic=lambda: next(clock, 31.0), admission_timeout_seconds=0.01,
            ).execute()
    assert access.read_calls == []
    access.assert_closed()
    assert not (tmp_path / "inputs/opened.json").exists()
    assert not (tmp_path / "out").exists()
    _assert_full_budget_available(budget)


@pytest.mark.parametrize("failure_at", ["none", "decode", "evaluation", "publication", "cancel_after_read"])
def test_terminal_reservation_and_descriptors_are_released_after_every_post_open_exit(tmp_path, monkeypatch, failure_at):
    parents = _Parents()
    data, plan = _inputs(tmp_path / "inputs", parents)
    access = _observe_outcome_reads(monkeypatch)
    budget = TrainingMemoryBudget(512 * 1024**2)
    publisher = TerminalConclusionPublisher(tmp_path / "out")

    def failed():
        raise ValueError("fixture_evaluation_failure")

    def failed_publication(_conclusion):
        raise OSError("fixture_publication_failure")

    if failure_at == "decode":
        (tmp_path / "inputs/tomorrow.jsonl").write_text("{broken\n")
    elif failure_at == "evaluation":
        monkeypatch.setattr("trader.training.application.terminal_holdout_execution.TomorrowPointInTimeHoldoutService.execute", lambda self: failed())
    elif failure_at == "publication":
        monkeypatch.setattr(publisher, "publish", failed_publication)
    use_case = _use_case(
        parents, data, publisher, plan, memory_admission=budget,
        cancel_requested=lambda: failure_at == "cancel_after_read" and bool(access.read_calls),
    )
    if failure_at == "none":
        assert use_case.execute().tomorrow.metrics.evaluated_rows == 200
    else:
        error = OSError if failure_at == "publication" else ResearchCancelledError if failure_at == "cancel_after_read" else ValueError
        with pytest.raises(error):
            use_case.execute()
    assert access.read_calls
    access.assert_closed()
    assert (tmp_path / "inputs/opened.json").is_file()
    _assert_full_budget_available(budget)
    marker = (tmp_path / "inputs/opened.json").read_bytes()
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        _open(data, data.inspect(), ("tomorrow", "d25"))
    assert (tmp_path / "inputs/opened.json").read_bytes() == marker


@pytest.mark.parametrize("replacement", ["rows", "plan"])
def test_descriptor_or_metadata_replacement_during_admission_is_rejected_before_opening(tmp_path, monkeypatch, replacement):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence = data.inspect()
    access = _observe_outcome_reads(monkeypatch)
    with pytest.raises(ValueError, match="regular_file_identity_changed|terminal metadata changed"):
        with data.prepare(evidence, ("tomorrow", "d25")) as prepared:
            if replacement == "rows":
                path = tmp_path / "inputs/tomorrow.jsonl"
                content = path.read_bytes()
                path.rename(path.with_suffix(".original"))
                path.write_bytes(content)
            else:
                path = tmp_path / "inputs/plan.json"
                payload = json.loads(path.read_text())
                payload["tomorrow"]["rows_sha256"] = "f" * 64
                path.write_text(json.dumps(payload))
            prepared.open_once()
    assert access.read_calls == []
    access.assert_closed()
    assert not (tmp_path / "inputs/opened.json").exists()


def test_terminal_inputs_are_typed_and_a_restart_cannot_reopen_them(tmp_path):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence = data.inspect()
    tomorrow, d25 = _open(data, evidence, ("tomorrow", "d25"))
    assert len(tomorrow) == len(d25) == 200
    assert tomorrow[0].actual_net_excess_returns == (0.018, 0.015, 0.01)
    assert len(d25[0].horizon_net_excess_returns) == 4
    marker = (tmp_path / "inputs/opened.json").read_bytes()
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        _open(TerminalHoldoutInputFiles(tmp_path / "inputs", tmp_path / "inputs/opened.json"), evidence, ("tomorrow", "d25"))
    assert (tmp_path / "inputs/opened.json").read_bytes() == marker


def test_concurrent_terminal_opening_has_only_one_reader(tmp_path, monkeypatch):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence, barrier = data.inspect(), Barrier(2)
    access = _observe_outcome_reads(monkeypatch)

    def open_rows():
        barrier.wait(timeout=5)
        try:
            return len(_open(data, evidence, ("tomorrow", "d25"))[0])
        except TerminalHoldoutAlreadyOpenedError:
            return -1

    with ThreadPoolExecutor(max_workers=2) as workers:
        attempts = [workers.submit(open_rows), workers.submit(open_rows)]
        assert sorted(attempt.result(timeout=10) for attempt in attempts) == [-1, 200]
    assert access.read_calls.count(tmp_path / "inputs/tomorrow.jsonl") == 201
    assert access.read_calls.count(tmp_path / "inputs/d25.jsonl") == 201
    access.assert_closed()


@pytest.mark.parametrize("corruption", ["digest", "daily_state"])
def test_invalid_content_burns_the_partition_and_a_second_attempt_reads_no_rows(tmp_path, monkeypatch, corruption):
    parents = _Parents()
    data, plan = _inputs(tmp_path / "inputs", parents)
    path = tmp_path / "inputs/tomorrow.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    row = json.loads(lines[0])
    row["capacity" if corruption == "digest" else "market_state"] = 10_001 if corruption == "digest" else "down"
    path.write_text(json.dumps(row, sort_keys=True) + "\n" + "".join(lines[1:]))
    access = _observe_outcome_reads(monkeypatch)
    reason = "outcomes differ from their sealed metadata" if corruption == "digest" else "terminal_daily_state_differs_from_registration"
    with pytest.raises(ValueError, match=reason):
        _use_case(parents, data, TerminalConclusionPublisher(tmp_path / "out"), plan).execute()
    assert access.read_calls
    access.assert_closed()
    marker = (tmp_path / "inputs/opened.json").read_bytes()
    read_count = len(access.read_calls)
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        _open(data, data.inspect(), ("tomorrow", "d25"))
    assert len(access.read_calls) == read_count
    assert (tmp_path / "inputs/opened.json").read_bytes() == marker
    assert not (tmp_path / "out").exists()


def test_missing_outcome_file_is_rejected_before_consuming_the_opening(tmp_path):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence = data.inspect()
    (tmp_path / "inputs/tomorrow.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        _open(data, evidence, ("tomorrow", "d25"))
    assert not (tmp_path / "inputs/opened.json").exists()


@pytest.mark.parametrize("conflict", ["plan", "parent", "dates", "missing_split", "reused_dates", "embargo"])
def test_terminal_prerequisites_reject_before_opening_missing_rows(tmp_path, conflict):
    parents = _Parents()
    data, plan = _inputs(tmp_path / "inputs", parents)
    if conflict == "plan":
        plan = replace(plan, settings=replace(plan.settings, v2_fit_date_candidates=(1000,)))
    elif conflict == "parent":
        parents.values = (replace(parents.values[0], candidate_hash="e" * 64), parents.values[1])
    elif conflict == "dates":
        plan = replace(plan, settings=replace(plan.settings, terminal_holdout_sessions=201))
    elif conflict == "missing_split":
        parents.values = (replace(parents.values[0], date_split=None), parents.values[1])
    elif conflict == "reused_dates":
        path = tmp_path / "inputs/plan.json"
        metadata = json.loads(path.read_text())
        metadata["tomorrow"]["trade_dates"] = [
            value.isoformat() for value in parents.values[0].date_split.training_dates[:200]
        ]
        path.write_text(json.dumps(metadata))
    else:
        plan = replace(plan, settings=replace(plan.settings, tomorrow_embargo_sessions=6))
    (tmp_path / "inputs/tomorrow.jsonl").unlink()
    with pytest.raises(ValueError):
        _use_case(parents, data, TerminalConclusionPublisher(tmp_path / "out"), plan).execute()
    assert not (tmp_path / "inputs/opened.json").exists()
    assert not (tmp_path / "out").exists()


def test_blocked_parents_publish_idempotently_without_inspecting_or_opening_inputs(tmp_path):
    data = TerminalHoldoutInputFiles(tmp_path / "missing-inputs", tmp_path / "parents/opened.json")
    publisher = TerminalConclusionPublisher(tmp_path / "out")
    use_case = _use_case(_Parents("historical_data_insufficient"), data, publisher, LoadResearchPlan(_CONFIG))
    first = use_case.execute()
    assert first == use_case.execute()
    assert not first.tomorrow.terminal_holdout_opened
    assert not first.d25.terminal_holdout_opened
    assert not (tmp_path / "missing-inputs").exists()


@pytest.mark.parametrize("blocked_profile", ["v3", None])
@pytest.mark.parametrize("parent_source", ["reader", "injected_port"])
def test_mixed_ready_and_blocked_parent_profiles_are_rejected_without_consuming_the_opening(tmp_path, monkeypatch, blocked_profile, parent_source):
    parents = _Parents()
    data, plan = _inputs(tmp_path / "inputs", parents)
    parents.values = (parents.values[0], replace(
        parents.values[1], candidate_status="historical_data_insufficient", failure_reasons=("fixture_blocked",),
        profile_id=blocked_profile, date_split=None, registration=None,
    ))
    parent_root = tmp_path / "parents"
    _seal_parents(parent_root, parents)
    parent_port = TerminalHoldoutParentsReader(parent_root, tmp_path / "inputs/opened.json") if parent_source == "reader" else parents
    access = _observe_outcome_reads(monkeypatch)
    (tmp_path / "inputs/tomorrow.jsonl").unlink()
    (tmp_path / "inputs/d25.jsonl").unlink()
    with pytest.raises(ValueError, match="terminal_strategy_profile_pair_mismatch"):
        _use_case(parent_port, data, TerminalConclusionPublisher(tmp_path / "out"), plan).execute()
    assert access.read_calls == access.descriptors == []
    assert not (tmp_path / "inputs/opened.json").exists()
    assert not (tmp_path / "out").exists()


def test_a_blocked_strategy_with_the_same_profile_preserves_the_qualified_strategy(tmp_path, monkeypatch):
    parents = _Parents()
    data, plan = _inputs(tmp_path / "inputs", parents)
    parents.values = (parents.values[0], replace(
        parents.values[1], candidate_status="historical_data_insufficient", failure_reasons=("fixture_blocked",),
        date_split=None, registration=None,
    ))
    parent_root = tmp_path / "parents"
    _seal_parents(parent_root, parents)
    parent_port = TerminalHoldoutParentsReader(parent_root, tmp_path / "inputs/opened.json")
    access = _observe_outcome_reads(monkeypatch)
    (tmp_path / "inputs/d25.jsonl").unlink()
    conclusion = _use_case(parent_port, data, TerminalConclusionPublisher(tmp_path / "out"), plan).execute()
    assert conclusion.tomorrow.terminal_holdout_opened
    assert not conclusion.d25.terminal_holdout_opened
    assert access.read_calls.count(tmp_path / "inputs/tomorrow.jsonl") == 201
    assert (tmp_path / "inputs/d25.jsonl") not in access.read_calls
    access.assert_closed()
    assert json.loads((tmp_path / "inputs/opened.json").read_text())["strategies"] == ["tomorrow"]


@pytest.mark.parametrize("profile_id", ["v1", "v2", "v3", "v5"])
def test_ready_bootstrap_consumes_real_fixture_rows_and_only_publication_can_repeat(tmp_path, profile_id):
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.bootstrap import ExecuteResearchEvidence
    from trader.training.application.research_commands import HoldoutCommand

    config_dir = tmp_path / "config"
    shutil.copytree(_CONFIG, config_dir)
    if profile_id == "v1":
        path = config_dir / "research.json"
        configuration = json.loads(path.read_text())
        next(item for item in configuration["fit_date_candidates"] if item["profile"] == "v1")["dates"] = [750]
        path.write_text(json.dumps(configuration))
    parents = _Parents(profile_id=profile_id, fit_dates=61 if profile_id == "v5" else 750)
    data, _plan = _inputs(tmp_path / "inputs", parents, config_dir=config_dir, vary_state=True)
    parent_root, output = tmp_path / "parents", tmp_path / "out"
    _seal_parents(parent_root, parents)
    command = HoldoutCommand(parent_root, tmp_path / "inputs", output)
    conclusion = ExecuteResearchEvidence(command, config_dir=config_dir, sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False)
    assert conclusion.tomorrow.metrics.evaluated_rows == conclusion.d25.metrics.evaluated_rows == 200
    assert conclusion.tomorrow.terminal_holdout_opened and conclusion.d25.terminal_holdout_opened
    assert conclusion.tomorrow.profile_id == conclusion.d25.profile_id == profile_id
    for report in (conclusion.tomorrow, conclusion.d25):
        assert report.status == "historical_rejected"
        assert "net_excess_not_positive" in report.failure_reasons
        assert "state_sample_below_40" not in report.failure_reasons
        assert all(count >= 40 for _label, count in report.metrics.state_sample_counts)
    original = (output / "cross_strategy/report.json").read_bytes()
    TerminalConclusionPublisher(output).publish(conclusion)
    assert (output / "cross_strategy/report.json").read_bytes() == original
    (tmp_path / "inputs/tomorrow.jsonl").unlink()
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        ExecuteResearchEvidence(command, config_dir=config_dir, sqlite_policy=SqlitePolicyFixture(), cancel_requested=lambda: False)
    assert data.inspect().research_plan_hash == _plan.content_hash


@pytest.mark.parametrize("failed_first_read", [False, True])
def test_cli_reopening_has_a_specific_error_even_after_a_burned_read(tmp_path, capsys, failed_first_read):
    from trader.entrypoints.cli import Main

    parents = _Parents()
    _inputs(tmp_path / "inputs", parents)
    parent_root = tmp_path / "parents"
    _seal_parents(parent_root, parents)
    if failed_first_read:
        (tmp_path / "inputs/tomorrow.jsonl").write_text("{broken\n")

    def run(evidence, output):
        return Main([
            "--config", str(_CONFIG / "runtime.json"), "research-terminal-holdout",
            "--parent-artifact-dir", str(parent_root), "--evidence-dir", str(evidence),
            "--output-dir", str(output),
        ])

    assert run(tmp_path / "inputs", tmp_path / "output") == (2 if failed_first_read else 1)
    first = json.loads(capsys.readouterr().out)
    assert first["production_authority"] is False
    if failed_first_read:
        assert first["error_code"] == "invalid_parent_artifacts"
    marker = (parent_root / "terminal_opened.json").read_bytes()
    assert run(tmp_path / "different-inputs", tmp_path / "different-output") == 2
    second = json.loads(capsys.readouterr().out)
    assert second["error_code"] == "terminal_holdout_already_opened"
    assert second["production_authority"] is False
    assert (parent_root / "terminal_opened.json").read_bytes() == marker
    assert not (tmp_path / "different-inputs").exists()
    assert not (tmp_path / "different-output").exists()


@pytest.mark.parametrize("conflict", ["missing_split", "null_ready_split", "overlapping_dates", "opened"])
def test_parent_codec_requires_the_complete_current_sealed_date_split(tmp_path, conflict):
    root = tmp_path / "parents"
    _seal_parents(root, _Parents())
    path = root / "terminal_parents.json"
    payload = json.loads(path.read_text())
    if conflict == "missing_split":
        del payload["tomorrow"]["date_split"]
    elif conflict == "null_ready_split":
        payload["tomorrow"]["date_split"] = None
    elif conflict == "overlapping_dates":
        split = payload["tomorrow"]["date_split"]
        split["training_dates"][-1] = split["calibration_dates"][0]
    else:
        payload["tomorrow"]["date_split"]["terminal_holdout_opened"] = True
    payload.pop("content_hash")
    payload["content_hash"] = CanonicalResearchHash(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        TerminalHoldoutParentsReader(root, root / "terminal_opened.json").read()
    assert not (root / "terminal_opened.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking input boundary")
@pytest.mark.parametrize("kind", ["parents", "plan", "rows"])
def test_terminal_codecs_reject_named_pipes_without_waiting_for_a_writer(tmp_path, kind):
    parents = _Parents()
    data, _plan = _inputs(tmp_path / "inputs", parents)
    evidence = data.inspect()
    parent_root = tmp_path / "parents"
    _seal_parents(parent_root, parents)
    path = {
        "parents": parent_root / "terminal_parents.json",
        "plan": tmp_path / "inputs/plan.json",
        "rows": tmp_path / "inputs/tomorrow.jsonl",
    }[kind]
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular_file_required"):
        if kind == "parents":
            TerminalHoldoutParentsReader(parent_root, parent_root / "terminal_opened.json").read()
        elif kind == "plan":
            data.inspect()
        else:
            _open(data, evidence, ("tomorrow", "d25"))
    assert not (tmp_path / "inputs/opened.json").exists()

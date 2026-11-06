from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from trader.training.application.terminal_holdout_execution import (
    ExecuteTerminalHoldout,
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

_CONFIG = Path(__file__).resolve().parents[4] / "config"


def _split():
    terminal = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(200))
    start = terminal[0] - timedelta(days=900)

    def dates(first, count):
        return tuple(start + timedelta(days=index) for index in range(first, first + count))

    return PointInTimeDateSplit(
        dates(0, 750), dates(750, 20), dates(770, 30), dates(800, 5),
        dates(805, 50), dates(855, 5), terminal,
    )


class _Parents:
    def __init__(self, status="historical_candidate_ready"):
        self.values = (
            TerminalHoldoutParentState(
                status, "a" * 64, "b" * 64, date_split=_split() if status == "historical_candidate_ready" else None,
            ),
            TerminalHoldoutParentState(
                status, "c" * 64, "d" * 64, date_split=_split() if status == "historical_candidate_ready" else None,
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
        }
        for strategy, parent in zip(("tomorrow", "d25"), parents.values, strict=True)
    }
    (root / "terminal_parents.json").write_text(
        json.dumps({**payload, "content_hash": CanonicalResearchHash(payload)}), encoding="utf-8"
    )


def _inputs(root, parents):
    root.mkdir()
    plan = LoadResearchPlan(_CONFIG)
    manifest = {"research_plan_hash": plan.content_hash}
    for strategy, parent in zip(("tomorrow", "d25"), parents.values, strict=True):
        encoded = []
        assert parent.date_split is not None
        dates = parent.date_split.terminal_holdout_dates
        for day in dates:
            row = TerminalHoldoutRow(
                day, "600001", "main", "fixture-industry", "up", "low", "high",
                0.02, (0.018, 0.015, 0.01), (0.008, 0.005, 0.0),
                False, False, False, False, -0.1, -0.1, False, False, 10_000, 10_000, True,
                (0.02, 0.02, 0.02, 0.02) if strategy == "d25" else (),
                (0.01, 0.01, 0.01, 0.01) if strategy == "d25" else (),
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
        }
    (root / "plan.json").write_text(json.dumps(manifest), encoding="utf-8")
    return TerminalHoldoutInputFiles(root, root / "opened.json"), plan


def test_terminal_inputs_are_typed_and_a_restart_cannot_reopen_them(tmp_path):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence = data.inspect()
    tomorrow, d25 = data.open_once(evidence, ("tomorrow", "d25"))
    assert len(tomorrow) == len(d25) == 200
    assert tomorrow[0].actual_net_excess_returns == (0.018, 0.015, 0.01)
    assert len(d25[0].horizon_net_excess_returns) == 4
    marker = (tmp_path / "inputs/opened.json").read_bytes()
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        TerminalHoldoutInputFiles(tmp_path / "inputs", tmp_path / "inputs/opened.json").open_once(evidence, ("tomorrow", "d25"))
    assert (tmp_path / "inputs/opened.json").read_bytes() == marker


def test_concurrent_terminal_opening_has_only_one_reader(tmp_path):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence, barrier = data.inspect(), Barrier(2)

    def open_rows():
        barrier.wait(timeout=5)
        try:
            return len(data.open_once(evidence, ("tomorrow", "d25"))[0])
        except TerminalHoldoutAlreadyOpenedError:
            return -1

    with ThreadPoolExecutor(max_workers=2) as workers:
        attempts = [workers.submit(open_rows), workers.submit(open_rows)]
        assert sorted(attempt.result(timeout=10) for attempt in attempts) == [-1, 200]


def test_a_failed_first_row_read_still_consumes_the_opening(tmp_path):
    data, _plan = _inputs(tmp_path / "inputs", _Parents())
    evidence = data.inspect()
    (tmp_path / "inputs/tomorrow.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        data.open_once(evidence, ("tomorrow", "d25"))
    assert (tmp_path / "inputs/opened.json").is_file()
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        data.open_once(evidence, ("tomorrow", "d25"))


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
        ExecuteTerminalHoldout(parents, data, TerminalConclusionPublisher(tmp_path / "out"), plan).execute()
    assert not (tmp_path / "inputs/opened.json").exists()
    assert not (tmp_path / "out").exists()


def test_blocked_parents_publish_idempotently_without_inspecting_or_opening_inputs(tmp_path):
    data = TerminalHoldoutInputFiles(tmp_path / "missing-inputs", tmp_path / "parents/opened.json")
    publisher = TerminalConclusionPublisher(tmp_path / "out")
    use_case = ExecuteTerminalHoldout(_Parents("historical_data_insufficient"), data, publisher, LoadResearchPlan(_CONFIG))
    first = use_case.execute()
    assert first == use_case.execute()
    assert not first.tomorrow.terminal_holdout_opened
    assert not first.d25.terminal_holdout_opened
    assert not (tmp_path / "missing-inputs").exists()


def test_ready_bootstrap_consumes_real_fixture_rows_and_only_publication_can_repeat(tmp_path):
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.bootstrap import ExecuteResearchEvidence
    from trader.training.application.research_commands import HoldoutCommand

    parents = _Parents()
    data, _plan = _inputs(tmp_path / "inputs", parents)
    parent_root, output = tmp_path / "parents", tmp_path / "out"
    _seal_parents(parent_root, parents)
    command = HoldoutCommand(parent_root, tmp_path / "inputs", output)
    conclusion = ExecuteResearchEvidence(command, config_dir=_CONFIG, sqlite_policy=SqlitePolicyFixture())
    assert conclusion.tomorrow.metrics.evaluated_rows == conclusion.d25.metrics.evaluated_rows == 200
    assert conclusion.tomorrow.terminal_holdout_opened and conclusion.d25.terminal_holdout_opened
    original = (output / "cross_strategy/report.json").read_bytes()
    TerminalConclusionPublisher(output).publish(conclusion)
    assert (output / "cross_strategy/report.json").read_bytes() == original
    (tmp_path / "inputs/tomorrow.jsonl").unlink()
    with pytest.raises(TerminalHoldoutAlreadyOpenedError):
        ExecuteResearchEvidence(command, config_dir=_CONFIG, sqlite_policy=SqlitePolicyFixture())
    assert data.inspect().research_plan_hash == _plan.content_hash


@pytest.mark.parametrize("failed_first_read", [False, True])
def test_cli_reopening_has_a_specific_error_even_after_a_burned_read(tmp_path, capsys, failed_first_read):
    from trader.entrypoints.cli import Main

    parents = _Parents()
    _inputs(tmp_path / "inputs", parents)
    parent_root = tmp_path / "parents"
    _seal_parents(parent_root, parents)
    if failed_first_read:
        (tmp_path / "inputs/tomorrow.jsonl").unlink()

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
        assert first["error_code"] == "artifact_io_failed"
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
            data.open_once(evidence, ("tomorrow", "d25"))
    assert (tmp_path / "inputs/opened.json").exists() is (kind == "rows")

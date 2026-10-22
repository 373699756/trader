from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from trader import bootstrap
from trader.entrypoints.cli import Main
from trader.training.application.selection_execution import ExecuteSelectionResearch
from trader.training.entrypoints.research_evidence import SelectionCommand
from trader.training.entrypoints.selection_projection import ProjectSelectionValidation
from trader.training.infra.research.selection_evidence import SelectionEvidenceReader
from trader.training.infra.research.selection_reports import PublishSelectionReport

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _dates(start: str, count: int) -> list[str]:
    return [(date.fromisoformat(start) + timedelta(days=index)).isoformat() for index in range(count)]


def _evidence(root: Path, *, days: int = 1, strategy: str = "tomorrow") -> dict:
    root.mkdir()
    dates = _dates("2025-06-02", days)
    spec = {
        "strategy": strategy,
        "profile_id": "v2",
        "dataset_hash": "a" * 64,
        "dataset_manifest_hash": "b" * 64,
        "candidate_recall_hash": "c" * 64,
        "industry_model_id": "industry_ridge",
        "industry_model_hash": "d" * 64,
        "pooled_ridge_model_id": "pooled_ridge",
        "pooled_ridge_model_hash": "e" * 64,
        "pooled_lightgbm_model_id": "pooled_lightgbm",
        "pooled_lightgbm_model_hash": "f" * 64,
        "research_dates": dates,
        "predefined_exit_identity": "first_target_or_t5",
        "partition": "confirmation",
        "configuration_hash": "1" * 64,
        "cost_reference_hash": "2" * 64,
        "bootstrap_master_seed": 20261022,
        "date_split": {
            "training_dates": ["2025-01-02"],
            "early_stopping_dates": ["2025-02-03"],
            "calibration_dates": ["2025-03-03"],
            "development_confirmation_embargo_dates": _dates("2025-04-01", 5),
            "confirmation_dates": dates,
            "confirmation_holdout_embargo_dates": _dates("2025-07-01", 5),
            "terminal_holdout_dates": _dates("2025-07-07", 200),
        },
    }
    rows = []
    parents = []
    for trade_date in dates:
        for index in range(9):
            parent = {"trade_date": trade_date, "code": f"600{index:03d}", "parent_row_hash": str(index + 1) * 64}
            parents.append(parent)
            targets = ("t1",) if strategy == "tomorrow" else ("t2", "t3", "t4", "t5", "predefined_exit")
            rows.append(
                {
                    **parent,
                    "board": ("main", "chinext", "star")[index % 3],
                    "industry": "same-industry",
                    "rule_score": 90 - index,
                    "estimated_round_trip_cost": 0.002,
                    "risk_eligible": True,
                    "action_eligible": True,
                    "capacity_sufficient": index != 0,
                    "execution_capacity": 100_000,
                    "severe_loss": False,
                    "mae_atr20": -0.2,
                    "point_in_time_parity": True,
                    "target_evidence": [
                        {
                            "target": target,
                            "industry_prediction_pre_cost": 0.02,
                            "pooled_ridge_prediction_pre_cost": 0.021,
                            "pooled_lightgbm_prediction_pre_cost": 0.022,
                            "actual_pre_cost_excess_return": 0.02 + position / 1000,
                            "exit_untradable": False,
                        }
                        for position, target in enumerate(targets)
                    ],
                }
            )
    encoded = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
    (root / "rows.jsonl").write_bytes(encoded)
    plan = {
        "specification": spec,
        "prerequisite": {
            "specification_hash": "0" * 64,
            "qualification_hash": "3" * 64,
            "dataset_status": "historical_point_in_time_parity",
            "candidate_recall_status": "candidate_recall_attributed",
            "dataset_hash": "a" * 64,
            "dataset_manifest_hash": "b" * 64,
            "candidate_recall_hash": "c" * 64,
            "point_in_time_parity": True,
            "terminal_holdout_opened": False,
            "population_rows": parents,
            "failure_reasons": [],
        },
        "rows_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    specification, _prerequisite = SelectionEvidenceReader(root).load_plan()
    plan["prerequisite"]["specification_hash"] = specification.content_hash
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return plan


def test_full_population_cli_is_offline_and_separates_six_slots_selected_mean_and_inference(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    _evidence(root)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", lambda: pytest.fail("offline study must not open HTTP"))
    monkeypatch.setattr(bootstrap, "BuildSystem", lambda *_a, **_kw: pytest.fail("must not build a server"))
    output = tmp_path / "validation.json"
    assert (
        Main(
            [
                "--config",
                str(PROJECT_ROOT / "config/runtime.json"),
                "research-selection",
                "--evidence-dir",
                str(root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text())
    assert payload["research_identity"] == "eleven_layer_full_population"
    assert len(payload["selection_contract"]["pipeline_stages"]) == 11
    assert payload["selection_contract"]["maximum_per_industry"] is None
    result = payload["results"][0]
    assert result["days"][0]["fixed_count"] == 6
    assert result["days"][0]["selected_count"] == 5
    assert result["mean_six_slot_net_excess_returns"][0] == pytest.approx(0.018 * 5 / 6)
    assert result["mean_selected_net_excess_returns"][0] == pytest.approx(0.018)
    assert payload["inference"][0]["invalid_reason"] == "sample_shorter_than_block"
    assert payload["production_authority"] is False
    assert payload["historical_acceptance"] == "not_granted"


def test_prerequisite_failure_reads_no_outcomes_even_if_the_row_file_is_missing(tmp_path):
    root = tmp_path / "evidence"
    plan = _evidence(root)
    plan["prerequisite"].update(
        {
            "dataset_status": "historical_data_insufficient",
            "candidate_recall_status": "historical_data_insufficient",
            "point_in_time_parity": False,
            "population_rows": [],
            "failure_reasons": ["minute_events_not_qualified"],
        }
    )
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (root / "rows.jsonl").unlink()
    execution = ExecuteSelectionResearch(SelectionEvidenceReader(root))
    assert execution.report.status == "historical_data_insufficient"
    assert "minute_events_not_qualified" in execution.report.failure_reasons
    assert execution.inference == ()


@pytest.mark.parametrize(
    "case", ["missing_row", "duplicate_row", "wrong_parent", "digest", "profile", "cost_reference", "old_capacity"]
)
def test_input_conflicts_never_produce_new_target_results(tmp_path, case):
    root = tmp_path / "evidence"
    plan = _evidence(root)
    rows = [json.loads(line) for line in (root / "rows.jsonl").read_text().splitlines()]
    if case == "missing_row":
        rows.pop()
    elif case == "duplicate_row":
        rows.append(rows[0])
    elif case == "wrong_parent":
        rows[0]["parent_row_hash"] = "8" * 64
    elif case == "profile":
        plan["specification"]["profile_id"] = "v3"
    elif case == "cost_reference":
        plan["specification"]["cost_reference_hash"] = "8" * 64
    elif case == "old_capacity":
        plan["specification"]["capacities"] = [120]
    encoded = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
    (root / "rows.jsonl").write_bytes(encoded)
    plan["rows_sha256"] = "0" * 64 if case == "digest" else hashlib.sha256(encoded).hexdigest()
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    if case == "old_capacity":
        with pytest.raises(ValueError, match="unknown keys"):
            ExecuteSelectionResearch(SelectionEvidenceReader(root))
    else:
        if case in {"profile", "cost_reference"}:
            (root / "rows.jsonl").unlink()
        execution = ExecuteSelectionResearch(SelectionEvidenceReader(root))
        assert execution.report.status == "historical_data_insufficient"
        assert execution.report.results == ()


def test_report_replay_is_idempotent_and_conflicting_content_does_not_overwrite(tmp_path):
    root = tmp_path / "evidence"
    _evidence(root)
    payload = ProjectSelectionValidation(bootstrap.ExecuteResearchEvidence(SelectionCommand(root)))
    path = tmp_path / "validation.json"
    PublishSelectionReport(path, payload)
    original = path.read_bytes()
    PublishSelectionReport(path, payload)
    with pytest.raises(RuntimeError, match="conflicts"):
        PublishSelectionReport(path, {**payload, "production_authority": True})
    assert path.read_bytes() == original


def test_preregistered_bootstrap_and_complete_holm_family_are_deterministic(tmp_path):
    root = tmp_path / "evidence"
    _evidence(root, days=5)
    first = ExecuteSelectionResearch(SelectionEvidenceReader(root))
    second = ExecuteSelectionResearch(SelectionEvidenceReader(root))
    assert first == second
    assert len(first.inference) == 16
    assert all(item.statistics.valid and item.statistics.repetitions == 10_000 for item in first.inference)
    assert {item.cost_bps for item in first.inference} == {20, 50}
    assert {item.holm.family_rank for item in first.inference} == set(range(1, 17))
    assert all(item.statistics.confidence_lower > 0 for item in first.inference)


def test_cli_rejects_input_output_overlap_without_writing_evidence(tmp_path, capsys):
    root = tmp_path / "evidence"
    _evidence(root)
    original = (root / "plan.json").read_bytes()
    result = Main(
        [
            "--config",
            str(PROJECT_ROOT / "config/runtime.json"),
            "research-selection",
            "--evidence-dir",
            str(root),
            "--output",
            str(root / "plan.json"),
        ]
    )
    assert result == 2
    assert (root / "plan.json").read_bytes() == original
    assert json.loads(capsys.readouterr().out)["production_authority"] is False


def test_d25_reports_four_costed_horizons_and_a_distinct_ten_day_bootstrap(tmp_path):
    root = tmp_path / "evidence"
    _evidence(root, strategy="d25")
    report = ProjectSelectionValidation(ExecuteSelectionResearch(SelectionEvidenceReader(root)))
    assert report["statistics_plan"]["block_days"] == 10
    assert report["selection_contract"]["production_target"] == "mean_t2_t5"
    aggregate = next(result for result in report["results"] if result["target"] == "mean_t2_t5")
    horizons = aggregate["days"][0]["horizon_six_slot_returns"]
    assert len(horizons) == 4
    assert aggregate["mean_six_slot_net_excess_returns"] == pytest.approx(
        [sum(horizon[index] for horizon in horizons) / 4 for index in range(3)]
    )
    assert all(item["valid"] is False for item in report["inference"])

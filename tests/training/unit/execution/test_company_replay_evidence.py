from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.company_replay_fixture import BuildCompanyReplayCase
from trader import bootstrap
from trader.entrypoints.cli import Main
from trader.recommendation.application.pipeline.final_selection.company_replay import ReplayCompanyDecisions
from trader.training.infra.research.company_replay_evidence import CompanyReplayEvidenceReader, ProjectCompanyReplayDay

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SIGNAL = datetime(2026, 7, 29, 15, 0, tzinfo=_SHANGHAI)
_MATURITY = datetime(2026, 8, 5, 15, 0, tzinfo=_SHANGHAI)
_SESSIONS = ["2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05"]
_CONFIG = Path(__file__).resolve().parents[4] / "config/runtime.json"


def _seal(path: Path, payload: object) -> str:
    encoded = (json.dumps(payload) + "\n").encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _write_evidence(root: Path, replay, *, evidence_kind: str = "fixture", omit_outcome: bool = False) -> None:
    projected = ProjectCompanyReplayDay(replay)
    codes = sorted({seat.code for arm in replay.arms for seat in arm.seats})
    outcomes = [
        {
            "code": code,
            "hit": code == "600000",
            "entry_tradable": True,
            "exit_untradable": False,
            "severe_loss": code == "600000",
            "pre_cost_excess_returns": [0.05] * 4 if code == "600000" else [0.01] * 4,
        }
        for code in codes if not (omit_outcome and code == "600000")
    ]
    replay_sha256 = _seal(root / "replays.jsonl", projected)
    outcomes_sha256 = _seal(root / "outcomes.jsonl", {
        "trade_date": _SESSIONS[0],
        "signal_identity": replay.signal_identity,
        "settled_at": (_MATURITY + timedelta(hours=1)).isoformat(),
        "maturity_date": _SESSIONS[-1],
        "prediction_target_bps": replay.prediction_target_bps,
        "outcomes": outcomes,
    })
    qualification_sha256 = _seal(root / "qualification.json", {
        "status": "qualified",
        "point_in_time_parity": True,
        "terminal_holdout_opened": False,
        "production_authority": False,
        "failure_reasons": [],
    })
    _seal(root / "plan.json", {
        "evidence_kind": evidence_kind,
        "profile": replay.profile,
        "strategy": replay.strategy.value,
        "prediction_target_bps": replay.prediction_target_bps,
        "trade_dates": [_SESSIONS[0]],
        "calendar_dates": _SESSIONS,
        "qualification_sha256": qualification_sha256,
        "replays_sha256": replay_sha256,
        "outcomes_sha256": outcomes_sha256,
    })


@pytest.fixture
def native_company_replay(application_feature_factory):
    inputs, policy, runtime, _observed = BuildCompanyReplayCase(application_feature_factory, evaluated_at=_SIGNAL)
    return ReplayCompanyDecisions(inputs, policy, runtime=runtime)


def _run(root: Path, monkeypatch, capsys, *, at: datetime = _MATURITY + timedelta(days=1)):
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: at)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", lambda: pytest.fail("offline replay must never open network"))
    monkeypatch.setattr(bootstrap, "BuildSystem", lambda *args: pytest.fail("offline replay must never start the service"))
    code = Main(["--config", str(_CONFIG), "research-company-replay", "--evidence-dir", str(root)])
    return code, json.loads(capsys.readouterr().out)


def test_company_replay_cli_evaluates_native_fixed_action_and_gate_pairs(
    tmp_path, monkeypatch, capsys, native_company_replay,
):
    _write_evidence(tmp_path, native_company_replay)
    code, payload = _run(tmp_path, monkeypatch, capsys)
    assert code == 0
    assert payload["status"] == "fixture_diagnostics_completed"
    assert payload["evidence_kind"] == "fixture"
    assert payload["production_authority"] is False
    assert payload["quality_accepted"] is False
    assert payload["terminal_holdout_opened"] is False
    diagnostics = payload["diagnostics"]
    seats = {(item["arm"], item["seat_group"]): item for item in diagnostics["seats"]}
    baseline = seats["baseline", "fixed"]
    assert baseline["seats"] == baseline["known_targets"] == 6
    assert baseline["hits"] == 1
    assert seats["risk", "fixed"]["hits"] == 0
    assert seats["baseline", "executable"]["seats"] == len([
        seat for seat in native_company_replay.arms[0].seats if seat.action.value == "executable"
    ])
    assert diagnostics["cost_scenarios_bps"] == [20, 50, 100]
    effect = diagnostics["paired_effects"][0]
    assert effect["comparison"] == "risk_minus_baseline"
    assert effect["paired_days"] == 1
    assert effect["mean_six_slot_net_excess_delta"] == pytest.approx([-0.04 / 6] * 3)
    gate = diagnostics["risk_gate_effects"][0]
    assert gate["newly_rejected_seats"] == gate["profitable_at_20bp"] == gate["severe_losses"] == 1
    assert gate["reason_counts"] == {"latest_disclosed_report_loss": 1}


@pytest.mark.parametrize("case", ["unproven_real_data", "before_fifth_market_close"])
def test_company_replay_refuses_outcomes_before_evidence_or_maturity(
    tmp_path, monkeypatch, capsys, native_company_replay, case,
):
    _write_evidence(
        tmp_path,
        native_company_replay,
        evidence_kind="point_in_time" if case == "unproven_real_data" else "fixture",
    )
    monkeypatch.setattr(
        CompanyReplayEvidenceReader,
        "_read_lines",
        lambda *_args: pytest.fail("must not open replay or outcomes"),
    )
    at = _MATURITY - timedelta(seconds=1) if case == "before_fifth_market_close" else _MATURITY + timedelta(days=1)
    code, payload = _run(tmp_path, monkeypatch, capsys, at=at)
    assert code == 1
    assert payload["status"] == "historical_data_insufficient"
    assert payload["diagnostics"] is None
    assert payload["failure_reasons"] == [
        "independent_point_in_time_replay_proof_unavailable"
        if case == "unproven_real_data"
        else "mature_replay_evidence_unavailable"
    ]


def test_company_replay_reports_partial_outcomes_without_imputing_paired_returns(
    tmp_path, monkeypatch, capsys, native_company_replay,
):
    _write_evidence(tmp_path, native_company_replay, omit_outcome=True)
    code, payload = _run(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert payload["status"] == "fixture_diagnostics_partial"
    diagnostics = payload["diagnostics"]
    baseline = diagnostics["seats"][0]
    assert baseline["seats"] == 6 and baseline["known_targets"] == 5
    assert baseline["mean_six_slot_net_excess_returns"] is None
    assert diagnostics["paired_effects"][0]["paired_days"] == 0
    assert diagnostics["risk_gate_effects"][0]["known_risk_outcomes"] == 0


def test_company_replay_reports_missing_evidence_without_running_a_research_holdout(tmp_path, monkeypatch, capsys):
    code, payload = _run(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert payload["failure_reasons"] == ["company_replay_plan_unavailable"]
    assert payload["terminal_holdout_opened"] is False
    assert list(tmp_path.iterdir()) == []

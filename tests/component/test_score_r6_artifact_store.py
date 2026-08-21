from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from trader.application.research.replay_models import canonical_hash, canonical_value
from trader.application.research.score_r6 import evaluate_score_r6_forward
from trader.application.research.score_r6_models import ScoreR6ForwardDay, ScoreR6ForwardPair
from trader.domain.research.score_r6 import (
    ScoreR6ForwardSpec,
    ScoreR6ProductionBoardWeights,
    ScoreR6ProductionCandidate,
)
from trader.infra.research.score_r6_artifacts import ScoreR6ArtifactConflictError, ScoreR6ArtifactStore


def _spec() -> ScoreR6ForwardSpec:
    registered = date(2026, 12, 1)
    planned = []
    current = registered
    while len(planned) < 20:
        current += timedelta(days=1)
        if current.weekday() < 5:
            planned.append(current)
    return ScoreR6ForwardSpec(
        "score_r6_forward_20261201_v1",
        registered,
        tuple(planned),
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
        "5" * 64,
    )


def test_r6_forward_spec_and_failed_days_are_immutable_and_tamper_evident(tmp_path) -> None:  # noqa: ANN001
    store = ScoreR6ArtifactStore(tmp_path)
    spec = _spec()
    day = ScoreR6ForwardDay(spec.content_hash, spec.planned_trade_dates[0], "failed", (), (), "source_unavailable")

    assert store.register_forward(spec) == spec.content_hash
    assert store.register_forward(spec) == spec.content_hash
    assert store.append_forward_day(spec, day) == day.content_hash
    assert store.append_forward_day(spec, day) == day.content_hash

    path = tmp_path / spec.research_identity / "days" / f"{day.trade_date}.json"
    path.write_text(path.read_text(encoding="utf-8").replace("source_unavailable", "service_stopped"), encoding="utf-8")
    with pytest.raises(ScoreR6ArtifactConflictError, match="hash"):
        store.append_forward_day(spec, day)


def test_r6_final_report_seal_binds_every_preregistered_day(tmp_path) -> None:  # noqa: ANN001
    store = ScoreR6ArtifactStore(tmp_path)
    spec = _spec()
    store.register_forward(spec)
    days = tuple(
        ScoreR6ForwardDay(spec.content_hash, trade_date, "failed", (), (), "source_unavailable")
        for trade_date in spec.planned_trade_dates
    )
    for day in days:
        store.append_forward_day(spec, day)
    report = evaluate_score_r6_forward(spec, days)

    assert report.status == "forward_rejected"
    assert store.seal_forward_report(spec, report) == report.content_hash
    assert store.inspect()["forward_research"] == [
        {
            "research_identity": spec.research_identity,
            "research_spec_hash": spec.content_hash,
            "recorded_days": 20,
            "report_hash": report.content_hash,
            "status": "forward_rejected",
            "promotion_eligible": False,
            "production_scope": "none",
        }
    ]

    report_path = tmp_path / spec.research_identity / "forward-report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["promotion_eligible"] = True
    unsigned = {key: value for key, value in payload.items() if key != "content_hash"}
    payload["content_hash"] = canonical_hash(unsigned)
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScoreR6ArtifactConflictError, match="schema"):
        store.inspect()


def test_r6_dossier_evidence_loader_reconstructs_all_bound_artifacts(tmp_path) -> None:  # noqa: ANN001
    names = ("tail_structure", "turnover_flow", "trend", "stability", "market_state", "entry_quality")
    candidate = ScoreR6ProductionCandidate(
        "a" * 64,
        tuple(
            ScoreR6ProductionBoardWeights(board, names, (1667, 556, 2222, 2777, 1111, 1667))
            for board in ("main", "chinext", "star")
        ),
        78,
        4,
    )
    historical = {"forward_candidate": canonical_value(candidate)}
    historical_hash = canonical_hash(historical)
    historical["content_hash"] = historical_hash
    historical_path = tmp_path / "score_r6_historical_v1" / "historical-report.json"
    historical_path.parent.mkdir(parents=True)
    historical_path.write_text(json.dumps(historical), encoding="utf-8")
    original = _spec()
    spec = ScoreR6ForwardSpec(
        original.research_identity,
        original.preregistered_on,
        original.planned_trade_dates,
        historical_hash,
        candidate.content_hash,
        original.trading_calendar_hash,
        original.rule_identity_hash,
        original.config_strategy_identity_hash,
    )
    days = tuple(
        ScoreR6ForwardDay(
            spec.content_hash,
            trade_date,
            "valid",
            tuple(
                ScoreR6ForwardPair(
                    f"60{index:04d}",
                    ("main", "chinext", "star")[index % 3],
                    0.2 if index < 5 else 0.0,
                    0.2 if 5 <= index < 10 else 0.0,
                    0.2 if 10 <= index < 15 else 0.0,
                    float(index),
                    False,
                )
                for index in range(15)
            ),
            tuple(f"60{index:04d}" for index in range(5, 10)),
            None,
        )
        for trade_date in spec.planned_trade_dates
    )
    report = evaluate_score_r6_forward(spec, days)
    store = ScoreR6ArtifactStore(tmp_path)
    store.register_forward(spec)
    for day in days:
        store.append_forward_day(spec, day)
    store.seal_forward_report(spec, report)

    loaded_spec, loaded_days, loaded_report, loaded_candidate = store.load_dossier_evidence(spec.research_identity)

    assert loaded_spec == spec
    assert loaded_days == days
    assert loaded_report == report
    assert loaded_candidate == candidate

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from trader.application.research.replay_models import canonical_hash
from trader.application.research.score_r6 import evaluate_score_r6_forward
from trader.application.research.score_r6_models import ScoreR6ForwardDay
from trader.domain.research.score_r6 import ScoreR6ForwardSpec
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

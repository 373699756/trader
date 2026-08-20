from __future__ import annotations

from dataclasses import replace

import pytest

from trader.application.research.challenger_models import (
    ChallengerCandidateOverride,
    ChallengerDayReplay,
    ChallengerSameStockPair,
)
from trader.application.research.score_r5_models import (
    ScoreR5ForwardBindings,
    ScoreR5ForwardDayRecord,
    score_r5_forward_dates,
)
from trader.domain.research.historical import CostSettlementBasis
from trader.domain.research.specification import SCORE_P0_V2_SPEC
from trader.infra.research.forward_evidence import ForwardEvidenceConflictError, JsonScoreR5ForwardStore


def _failed_record(reason: str = "source_unavailable") -> ScoreR5ForwardDayRecord:
    bindings = ScoreR5ForwardBindings(
        "a" * 64,
        "continuous_entry",
        "continuous_entry_v1",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
    )
    return ScoreR5ForwardDayRecord(
        bindings,
        score_r5_forward_dates()[0],
        "failed",
        None,
        (),
        reason,
    )


def test_score_r5_forward_store_is_idempotent_and_rejects_conflicts(tmp_path) -> None:  # noqa: ANN001
    store = JsonScoreR5ForwardStore(tmp_path)
    record = _failed_record()

    first = store.append(record)
    second = store.append(record)

    assert first == second == record
    assert store.read("continuous_entry", record.planned_trade_date) == record
    with pytest.raises(ForwardEvidenceConflictError, match="identity conflict"):
        store.append(replace(record, failure_reason="service_stopped"))


def test_score_r5_forward_store_detects_tampering(tmp_path) -> None:  # noqa: ANN001
    store = JsonScoreR5ForwardStore(tmp_path)
    record = store.append(_failed_record())
    path = tmp_path / "continuous_entry" / f"{record.planned_trade_date.isoformat()}.json"
    path.write_text(path.read_text(encoding="utf-8").replace("source_unavailable", "changed"), encoding="utf-8")

    with pytest.raises(ForwardEvidenceConflictError, match="hash or schema"):
        store.read("continuous_entry", record.planned_trade_date)


def test_score_r5_forward_store_round_trips_valid_no_decision_evidence(tmp_path) -> None:  # noqa: ANN001
    store = JsonScoreR5ForwardStore(tmp_path)
    failed = _failed_record()
    trade_date = score_r5_forward_dates()[1]
    code = "600001"
    override = ChallengerCandidateOverride(code, None, "not_enabled", None, False, False, False, ())
    pair = ChallengerSameStockPair(
        code,
        "main",
        None,
        None,
        None,
        0.0,
        0.0,
        0.0,
        50.0,
        50.0,
        "control_copy",
        CostSettlementBasis(code, "main", trade_date, trade_date.replace(day=trade_date.day + 1), 0.0, 0.0, 0.0),
    )
    day = ChallengerDayReplay(trade_date, "1" * 64, "2" * 64, (override,), (pair,), "no_decision", "no_decision")
    record = ScoreR5ForwardDayRecord(failed.bindings, trade_date, "no_decision", day, (), None)

    assert store.append(record) == record
    assert store.read("continuous_entry", trade_date) == record


def test_score_r5_forward_store_namespaces_the_new_research_identity(tmp_path) -> None:  # noqa: ANN001
    store = JsonScoreR5ForwardStore(tmp_path, spec=SCORE_P0_V2_SPEC)
    bindings = replace(
        _failed_record().bindings,
        research_identity="score_p0_v2",
        research_spec_hash=SCORE_P0_V2_SPEC.content_hash,
        statistics_version="score_r5_paired_mbb_holm_v2",
        report_version="score_r5_final_report_v2",
    )
    record = replace(
        _failed_record(),
        bindings=bindings,
        planned_trade_date=SCORE_P0_V2_SPEC.forward_dates[0],
        schema_version="score_r5_forward_day_v2",
    )

    assert store.append(record) == record
    assert store.read("continuous_entry", record.planned_trade_date) == record
    assert (tmp_path / "score_p0_v2" / "continuous_entry" / "2026-10-26.json").is_file()

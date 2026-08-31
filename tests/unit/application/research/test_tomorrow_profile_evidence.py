from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trader.application.research.tomorrow_profile_reporting import TomorrowProfileReportingService
from trader.application.research.tomorrow_profile_settlement import (
    TomorrowProfileSettlementDependencies,
    TomorrowProfileSettlementService,
)
from trader.domain.outcome.models import BenchmarkReturn, OutcomeBar
from trader.domain.research.tomorrow_profile_comparison import (
    TOMORROW_PROFILE_COMPARISON_SPEC,
    TomorrowProfilePair,
    TomorrowProfilePairManifest,
    TomorrowProfilePrediction,
)
from trader.infra.persistence.tomorrow_profile_comparison import SQLiteTomorrowProfileEvidenceStore

TRADE_DATE = date(2026, 8, 31)
OBSERVED_AT = datetime(2026, 8, 31, 14, 49, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
SETTLED_AT = datetime(2026, 9, 1, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _prediction(profile_id: str) -> TomorrowProfilePrediction:
    return TomorrowProfilePrediction(
        profile_id=profile_id,  # type: ignore[arg-type]
        model_version=f"model-{profile_id}",
        predicted_excess_return_pct=0.8,
        estimated_cost_pct=0.2,
        predicted_net_excess_pct=0.6,
        signal_score=70.0,
        local_score=70.0,
        model_disagreement_pct=0.0,
        action="unavailable",
        selected=False,
        rank=0,
    )


def _manifest() -> TomorrowProfilePairManifest:
    pairs = tuple(
        TomorrowProfilePair(
            input_version="native:formal",
            trade_date=TRADE_DATE,
            code=code,
            board="main",
            industry="工业",
            anchor_price=10.0,
            atr20_pct=atr,
            v1=_prediction("v1"),
            v2=_prediction("v2"),
        )
        for code, atr in (("600001", 2.0), ("600002", None))
    )
    return TomorrowProfilePairManifest(
        spec_hash=TOMORROW_PROFILE_COMPARISON_SPEC.content_hash,
        input_version="native:formal",
        trade_date=TRADE_DATE,
        observed_at=OBSERVED_AT,
        active_profile_id="v1",
        v1_model_version="model-v1",
        v2_model_version="model-v2",
        common_candidate_count=2,
        v1_scorable_count=2,
        v2_scorable_count=2,
        pairs=pairs,
    )


class _MarketData:
    def read_outcome_bars(self, codes: tuple[str, ...], _now: datetime):
        bars = (
            OutcomeBar("2026-08-31", 10.0, 10.1, 9.9, 10.0, 0.0),
            OutcomeBar("2026-09-01", 10.0, 10.3, 9.9, 10.2, 2.0),
        )
        return {code: bars for code in codes}


class _Decisions:
    def list_dates(self, _strategy, *, limit: int):
        assert limit == 31
        return (TRADE_DATE,)

    def load(self, _strategy, trade_date: date):
        assert trade_date == TRADE_DATE
        return SimpleNamespace(
            version="record:formal",
            committed_at=OBSERVED_AT,
            decision=SimpleNamespace(input_versions=(("native", "native:formal"),)),
        )


class _Benchmarks:
    def benchmark_returns_after(self, recommend_date: str, *, limit: int):
        assert recommend_date == "2026-08-31"
        assert limit == 1
        return (BenchmarkReturn("2026-09-01", 0.5),)


def _session_distance(start: str, end: str) -> int | None:
    values = {("2026-08-31", "2026-09-01"): 1}
    return values.get((start, end))


def test_formal_pair_settlement_keeps_zero_selection_and_marks_missing_atr_explicitly(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    store.save_manifest(_manifest())
    service = TomorrowProfileSettlementService(
        TomorrowProfileSettlementDependencies(
            _MarketData(),  # type: ignore[arg-type]
            _Decisions(),  # type: ignore[arg-type]
            _Benchmarks(),  # type: ignore[arg-type]
            store,
        ),
        session_distance=_session_distance,
    )

    result = service.settle(SETTLED_AT, ())

    assert result.formal_bound_count == 1
    assert result.target_count == 2
    assert result.outcome_count == 2
    assert result.complete_count == 1
    assert store.status().formal_manifests == 1
    assert store.status().settled_pairs == 2
    assert store.status().complete_pairs == 1
    assert tuple(item.stock_code for item in store.complete_outcomes()) == ("600001",)


def test_two_layer_report_counts_all_complete_candidates_when_both_profiles_select_zero(tmp_path: Path) -> None:
    reporting_spec = replace(TOMORROW_PROFILE_COMPARISON_SPEC, minimum_paired_candidates=1)
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, reporting_spec)
    store.save_manifest(replace(_manifest(), spec_hash=reporting_spec.content_hash))
    store.bind_formal_input(
        trade_date=TRADE_DATE,
        input_version="native:formal",
        record_version="record:formal",
        committed_at=OBSERVED_AT,
    )
    service = TomorrowProfileSettlementService(
        TomorrowProfileSettlementDependencies(
            _MarketData(),  # type: ignore[arg-type]
            _Decisions(),  # type: ignore[arg-type]
            _Benchmarks(),  # type: ignore[arg-type]
            store,
        ),
        session_distance=_session_distance,
    )
    service.settle(SETTLED_AT, ())

    report = TomorrowProfileReportingService(reporting_spec, store).report()

    assert report.state == "collecting"
    assert report.independent_days == 1
    assert report.paired_candidates == 1
    assert report.v1.candidate_pairs == 1
    assert report.v2.candidate_pairs == 1
    assert report.v1.mean_portfolio_net_excess_20bp_pct == 0.0
    assert report.v2.mean_portfolio_net_excess_20bp_pct == 0.0
    assert report.daily_v2_minus_v1_20bp_pct == (0.0,)
    assert report.production_authority is False
    assert report.automatic_profile_switch is False


def test_partial_cross_section_is_not_counted_as_an_independent_day(tmp_path: Path) -> None:
    store = SQLiteTomorrowProfileEvidenceStore(tmp_path, TOMORROW_PROFILE_COMPARISON_SPEC)
    store.save_manifest(_manifest())
    service = TomorrowProfileSettlementService(
        TomorrowProfileSettlementDependencies(
            _MarketData(),  # type: ignore[arg-type]
            _Decisions(),  # type: ignore[arg-type]
            _Benchmarks(),  # type: ignore[arg-type]
            store,
        ),
        session_distance=_session_distance,
    )

    service.settle(SETTLED_AT, ())
    report = TomorrowProfileReportingService(TOMORROW_PROFILE_COMPARISON_SPEC, store).report()

    assert store.status().settled_pairs == 2
    assert store.status().complete_pairs == 1
    assert store.status().independent_days == 0
    assert report.independent_days == 0
    assert report.paired_candidates == 0

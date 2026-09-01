from datetime import date, timedelta

from trader.application.research.historical_candidate_confirmation import (
    HistoricalStrategyResearchRequest,
    execute_codex_b_batch,
    execute_historical_strategy_research,
)
from trader.domain.research.filter_recall_ablation import FilterAblationRow


def _dates(start: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range(10))


def _rows(dates: tuple[date, ...], *, offset: int) -> tuple[FilterAblationRow, ...]:
    return tuple(
        FilterAblationRow(
            trade_date=trade_date,
            code=f"60{offset + day_index * 10 + stock_index:04d}",
            board="main",
            industry="shared",
            permanent_eligible=True,
            safety_veto=False,
            evidence_complete=stock_index != 0,
            candidate_present=True,
            candidate_reliable=True,
            candidate_score=80.0,
            candidate_rank=stock_index + 1,
            actual_net_excess_20bp=0.02 if stock_index == 0 else 0.005,
            actual_net_excess_50bp=0.017 if stock_index == 0 else 0.002,
            severe_loss=False,
            predicted_severe_loss_risk=0.05,
            capacity=1.0,
        )
        for day_index, trade_date in enumerate(dates)
        for stock_index in range(10)
    )


def _request(strategy: str, *, code_offset: int = 0) -> HistoricalStrategyResearchRequest:
    development = _dates(date(2024, 1, 1))
    confirmation = _dates(date(2024, 2, 1))
    return HistoricalStrategyResearchRequest(
        strategy=strategy,  # type: ignore[arg-type]
        parent_split_hash="a" * 64,
        parent_residual_ledger_hash="b" * 64,
        development_dates=development,
        confirmation_dates=confirmation,
        development_rows=_rows(development, offset=code_offset),
        confirmation_rows=_rows(confirmation, offset=code_offset),
    )


def test_strategy_research_seals_one_candidate_and_keeps_terminal_holdout_closed() -> None:
    result = execute_historical_strategy_research(_request("tomorrow"), repetitions=100)

    assert result.status == "historical_candidate_ready"
    assert result.candidate_report.selected_candidate_id == "tomorrow_observe_evidence"
    assert result.confirmation_report.selected_candidate_id == "tomorrow_observe_evidence"
    assert result.terminal_holdout_status == "terminal_holdout_not_opened"
    assert result.production_authority is False
    assert len(result.content_hash) == 64


def test_batch_returns_three_independent_strategy_terminal_states() -> None:
    batch = execute_codex_b_batch(
        (
            _request("today", code_offset=0),
            _request("tomorrow", code_offset=200),
            _request("d25", code_offset=400),
        ),
        repetitions=100,
    )

    assert tuple(item.strategy for item in batch.strategies) == ("today", "tomorrow", "d25")
    assert all(item.terminal_holdout_status == "terminal_holdout_not_opened" for item in batch.strategies)
    assert batch.production_authority is False
    assert len(batch.content_hash) == 64


def test_rejected_parent_is_inherited_without_fabricating_confirmation_evidence() -> None:
    request = _request("tomorrow")
    request = HistoricalStrategyResearchRequest(
        strategy=request.strategy,
        parent_split_hash=request.parent_split_hash,
        parent_residual_ledger_hash=request.parent_residual_ledger_hash,
        development_dates=request.development_dates,
        confirmation_dates=request.confirmation_dates,
        development_rows=(),
        confirmation_rows=(),
        parent_status="historical_data_insufficient",
        parent_failure_reasons=("residual_ledger_unavailable",),
    )

    result = execute_historical_strategy_research(request, repetitions=100)

    assert result.status == "historical_data_insufficient"
    assert result.confirmation_report.evidence == ()
    assert result.confirmation_report.failure_reasons == ("residual_ledger_unavailable",)
    assert result.terminal_holdout_status == "terminal_holdout_not_opened"


def test_development_rejection_cannot_be_reselected_on_confirmation_data() -> None:
    request = _request("tomorrow")
    concentrated = tuple(
        FilterAblationRow(
            **{
                **row.__dict__,
                "code": "609999" if not row.evidence_complete else row.code,
            }
        )
        for row in request.development_rows
    )
    request = HistoricalStrategyResearchRequest(
        strategy=request.strategy,
        parent_split_hash=request.parent_split_hash,
        parent_residual_ledger_hash=request.parent_residual_ledger_hash,
        development_dates=request.development_dates,
        confirmation_dates=request.confirmation_dates,
        development_rows=concentrated,
        confirmation_rows=request.confirmation_rows,
    )

    result = execute_historical_strategy_research(request, repetitions=100)

    assert result.candidate_report.selected_candidate_id is None
    assert result.status == "historical_rejected"
    assert result.confirmation_report.evidence == ()
    assert result.confirmation_report.failure_reasons == ("development_candidate_not_selected",)


def test_ready_parent_without_research_rows_closes_as_data_insufficient() -> None:
    request = _request("today")
    request = HistoricalStrategyResearchRequest(
        strategy=request.strategy,
        parent_split_hash=request.parent_split_hash,
        parent_residual_ledger_hash=request.parent_residual_ledger_hash,
        development_dates=request.development_dates,
        confirmation_dates=request.confirmation_dates,
        development_rows=(),
        confirmation_rows=(),
    )

    result = execute_historical_strategy_research(request, repetitions=100)

    assert result.candidate_report.status == "historical_data_insufficient"
    assert result.status == "historical_data_insufficient"
    assert result.confirmation_report.evidence == ()
    assert result.confirmation_report.failure_reasons == ("development_data_insufficient",)

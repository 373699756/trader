from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.diagnose_runtime import (
    DiagnosticResult,
    _parser,
    _validate,
    build_commands,
    build_report,
)
from scripts.runtime_diagnostics import market_factor_auxiliary as auxiliary
from scripts.runtime_diagnostics import market_factor_compare as comparison
from scripts.runtime_diagnostics import market_factor_eastmoney as eastmoney_probe
from scripts.runtime_diagnostics.market_factor_audit import FactorAudit, MissingQuoteCodes, QuoteAudit
from scripts.runtime_diagnostics.market_factor_baostock import BaostockCapability, BaoStockSourceResults
from scripts.runtime_diagnostics.market_factor_budget import DiagnosticBudget
from scripts.runtime_diagnostics.market_factor_inputs import (
    ActiveModelHead,
    FixedReferences,
    HistoryAtCut,
    ReadReferences,
)
from scripts.runtime_diagnostics.market_factor_text import RenderMarketFactorText
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.infra.market_data.observations import SourceObservation
from trader.infra.settings import LoadStrategySettings
from trader.infra.settings.data_paths import DataPaths
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.models import Board, FeatureSnapshot, MarketQuote, ModelIndustryReference
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.market_data.providers.eastmoney_spot import ParseEastmoneySpotQuote
from trader.recommendation.infra.policy_settings import _recommendation_policy

AT = datetime.fromisoformat("2026-10-15T14:30:00+08:00")


def _quote(code: str = "600001", **changes: object) -> MarketQuote:
    quote = MarketQuote(
        code=code,
        name="fixture",
        price=10,
        previous_close=9,
        open_price=9,
        high=10,
        low=9,
        pct_change=1,
        change_5m=0,
        speed=0,
        volume_ratio=1,
        turnover_rate=1,
        amount=100_000_000,
        volume=10_000_000,
        amplitude=1,
        market_cap=10_000_000_000,
        industry="industry",
        source="tencent",
        source_time=AT - timedelta(seconds=1),
        received_time=AT,
        data_version="fixture",
        board=Board.MAIN,
    )
    return replace(quote, **changes)


def test_official_reference_failure_uses_only_validated_local_population() -> None:
    class FailingClient:
        def fetch(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
            raise RuntimeError("exchange_unavailable")

    row = SourceObservation(
        source="exchange_security_master",
        subject_key="600001",
        observed_at=AT,
        source_time=AT,
        received_at=AT,
        effective_at=AT,
        data_version="fixture",
        fields={"board": "main", "listing_date": "2000-01-01"},
        missing_reasons={},
        payload_hash="",
        status="success",
        error_code=None,
    )
    cached = tuple(replace(row, subject_key=f"{code:06d}") for code in range(4_000))
    official, mode = comparison._official_references(FailingClient(), AT, cached)
    assert len(official) == 4_000
    assert mode == "cached_exchange_degraded"
    with pytest.raises(RuntimeError, match="exchange_unavailable"):
        comparison._official_references(FailingClient(), AT, cached[:3_999])


def test_coverage_keeps_missing_requested_rows_and_valid_zero_values() -> None:
    report = QuoteAudit((_quote(), _quote("600009")), ("600001", "600002"), AT)
    fields = {item["field"]: item for item in report["fields"]}
    assert report["returned"] == 1
    assert "speed" not in fields
    assert "change_5m" not in fields
    assert "industry" not in fields
    assert fields["price"]["coverage"] == 0.5
    assert report["core_complete"] == 1


def test_quote_age_never_treats_received_time_or_future_time_as_verified_freshness() -> None:
    quotes = (
        _quote(),
        _quote("600002", source="sina"),
        _quote("600003", source_time=AT),
        _quote("600004", source_time=AT + timedelta(seconds=1)),
    )
    report = QuoteAudit(quotes, tuple(q.code for q in quotes), AT)
    assert report["source_timestamp_available"] == 2
    assert report["fresh_source_timestamp_rows"] == 1
    assert report["future_timestamp_rows"] == 1


def test_supplementation_ignores_optional_speed_but_keeps_decision_gaps() -> None:
    quotes = (_quote(speed=None), _quote("600002", turnover_rate=None))
    assert MissingQuoteCodes(quotes, ("600001", "600002", "600003"), AT) == ("600002", "600003")


def test_supplementation_uses_local_industry_without_network_gap_fill() -> None:
    assert MissingQuoteCodes((_quote(industry=""),), ("600001",), AT) == ()


def test_factor_audit_counts_only_requested_histories_and_derived_local_factors() -> None:
    policy = _recommendation_policy(LoadStrategySettings(Path("config/strategy.json")))
    inputs = SimpleNamespace(
        profile="v3",
        policy=policy,
        histories={"600001": (1,) * 61, "600009": (1,) * 61},
        history_sessions=61,
        models=SimpleNamespace(
            history_required_sessions=lambda strategy: 61, is_input_eligible=lambda strategy, item: False
        ),
        model_heads=(
            ActiveModelHead(Strategy.TOMORROW, ("qfq_return_1d",), True),
            ActiveModelHead(Strategy.D25, ("qfq_return_3d",), True),
        ),
    )
    feature = FeatureSnapshot(_quote(), {"amount_median_20d": 100_000_000, "turnover_median_20d": 1}, AT)
    report = FactorAudit((feature,), inputs, ("600001", "600002"))
    fields = {item["field"]: item for item in report["fields"]}
    assert report["qfq_complete_rows"] == 1
    assert report["qfq_missing_rows"] == 1
    assert report["qfq_missing_codes"] == ["600002"]
    assert report["qfq_length_distribution"] == {"61": 1, "0": 1}
    assert report["qfq_one_session_short_rows"] == 0
    assert report["qfq_short_windows"] == {}
    assert all(
        model["input_reason_counts"] == {"model_industry_missing": 1, "qfq_window_missing": 1}
        for model in report["models"]
    )
    assert fields["amount_shock_score"]["valid"] == 1
    assert fields["amount_shock_score"]["missing"] == 1
    model_fields = {
        model["strategy"]: [item["field"] for item in model["feature_contract"]] for model in report["models"]
    }
    assert model_fields == {"tomorrow": ["qfq_return_1d"], "d25": ["qfq_return_3d"]}
    assert all(model["requires_model_industry"] is True for model in report["models"])
    assert not report["required_factors_complete"]


def test_failed_or_incomplete_rounds_never_count_as_complete_latency() -> None:
    samples = [
        {"profile": "v3", "scope": 120, "route": "tencent", "status": "source_failed", "total_ms": 1.0},
        {
            "profile": "v3",
            "scope": 120,
            "route": "tencent",
            "status": "measured",
            "input_seal_ms": 2.0,
            "raw_quote_complete": True,
            "factor_complete": False,
        },
    ]
    report = comparison.Summarize(samples)[0]
    assert report["measured_rounds"] == 1
    assert report["factor_complete_rounds"] == 0
    assert report["complete_input_timings_ms"]["sample_count"] == 0


def test_backup_fetches_full_scope_only_when_primary_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fetch(source, codes, pool, timeout, deadline):
        calls.append((source, codes, deadline))
        return comparison.SourceAttempt(source, (_quote(turnover_rate=None), _quote("600002")), 1, 2, None)

    monkeypatch.setattr(comparison, "FetchSource", fetch)
    plan = comparison.RoutePlan("tencent_then_sina", ("tencent", "sina"), "sequential_failure_backup")
    batch = comparison.AcquireBatch(
        plan,
        ("600001", "600002"),
        {"tencent": None, "sina": None},
        Namespace(batch_seconds=5, timeout_seconds=1),
        DiagnosticBudget(120, 600),
    )
    assert len(batch.attempts) == 2
    assert calls[1][1] == ("600001", "600002")
    assert calls[0][2] <= calls[1][2]


def test_successful_primary_does_not_start_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fetch(source, codes, pool, timeout, deadline):
        calls.append(source)
        return comparison.SourceAttempt(source, (_quote(),), 1, 1, None)

    monkeypatch.setattr(comparison, "FetchSource", fetch)
    plan = comparison.RoutePlan("tencent_then_sina", ("tencent", "sina"), "sequential_failure_backup")
    batch = comparison.AcquireBatch(
        plan,
        ("600001",),
        {"tencent": None, "sina": None},
        Namespace(batch_seconds=1, timeout_seconds=1),
        DiagnosticBudget(120, 600),
    )
    assert [item.source for item in batch.attempts] == ["tencent"]
    assert calls == ["tencent"]


def test_market_factor_profile_uses_unified_output_and_explicit_bounds() -> None:
    parser = _parser()
    args = parser.parse_args(["--profile", "market-factor-compare", "--market-sizes", "120", "--market-mode", "single"])
    options, _output = _validate(args)
    command = build_commands(options)[0]
    assert command.name == "market_factor_compare"
    assert "scripts.runtime_diagnostics.market_factor_compare" in command.argv
    assert "--batch-seconds" in command.argv
    assert command.argv[command.argv.index("--source-loop-seconds") + 1] == "120.0"
    assert command.argv[command.argv.index("--task-seconds") + 1] == "600.0"
    assert command.timeout_seconds == 180.0
    assert "--output" not in command.argv
    assert command.argv[command.argv.index("--sources") + 1 : command.argv.index("--profiles")] == (
        "tencent",
        "eastmoney",
        "sina",
        "baostock",
    )
    assert options.output_format == "text"

    file_args = parser.parse_args(["--profile", "market-factor-compare", "--output", "/tmp/market-factor-result.json"])
    file_options, _output = _validate(file_args)
    assert file_options.output_format == "json"

    long_args = parser.parse_args(["--profile", "market-factor-compare", "--command-timeout-seconds", "1800"])
    long_options, _output = _validate(long_args)
    assert build_commands(long_options)[0].timeout_seconds == 600.0

    with pytest.raises(ValueError, match="market-task-seconds"):
        _validate(parser.parse_args(["--profile", "market-factor-compare", "--market-task-seconds", "601"]))

    with pytest.raises(ValueError, match="Tencent"):
        _validate(
            parser.parse_args(
                [
                    "--profile",
                    "market-factor-compare",
                    "--market-sources",
                    "eastmoney",
                    "sina",
                    "--market-mode",
                    "all",
                ]
            )
        )


def test_reference_probe_never_initializes_missing_runtime_database(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="published_reference_unavailable"):
        ReadReferences(tmp_path, AT)
    assert not tuple(tmp_path.iterdir())


def test_history_cut_excludes_unfinished_and_future_bars_but_accepts_today_after_close() -> None:
    bars = tuple(
        DailyBar(
            trade_date=day,
            open_price=10,
            close=10,
            high=10,
            low=10,
            volume=100,
            amount=1000,
            pct_change=0,
            turnover_rate=1,
            adjustment=PriceAdjustment.QFQ,
            source="fixture",
        )
        for day in ("2026-10-14", "2026-10-15", "2026-10-16")
    )
    assert len(HistoryAtCut(bars, AT)) == 1
    assert len(HistoryAtCut(bars, AT.replace(hour=15))) == 2


def test_profiles_share_exactly_one_acquisition_per_route_and_round(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = comparison.CollectedBatch((), (), AT, 10)
    acquired = []
    replayed = []
    industry = ModelIndustryReference("制造业", "证监会行业分类", AT.date(), "baostock", "fixture")
    references = FixedReferences((), {"600001": industry}, (), ())
    monkeypatch.setattr(
        comparison,
        "LoadRuntimeSettings",
        lambda path: SimpleNamespace(runtime_dir=path, strategy_config_path=path, data_paths=DataPaths(path)),
    )
    monkeypatch.setattr(
        comparison,
        "SQLiteIssuerEligibilityIndex",
        lambda *args, **kwargs: SimpleNamespace(
            status=lambda: SimpleNamespace(
                schema_version="fixture",
                fact_count=0,
                excluded_count=0,
                integrity_ok=True,
                refresh_state="ready",
                last_error=None,
            ),
            filter_codes=lambda codes, at: tuple(codes),
        ),
    )
    monkeypatch.setattr(comparison, "ReadReferences", lambda path, at: references)
    monkeypatch.setattr(
        comparison, "ExchangeSecurityMasterClient", lambda **kwargs: SimpleNamespace(fetch=lambda at: ())
    )
    monkeypatch.setattr(comparison, "LoadStrategySettings", lambda path: SimpleNamespace(long_research=None))
    monkeypatch.setattr(comparison, "ProbeAuxiliary", lambda *args, **kwargs: SimpleNamespace(report=()))
    monkeypatch.setattr(
        comparison,
        "LoadFactorInputs",
        lambda config, qfq, profile, codes, at: SimpleNamespace(
            profile=profile, snapshot_hash="fixed", config_version="fixed", preparation_ms={}
        ),
    )

    def acquire(*args):
        acquired.append(args)
        return batch

    def replay(
        plan, codes, inputs, references, official, received, args, number, size, auxiliary, *, cpu_profile_dir=None
    ):
        replayed.append((received, references))
        return {
            "profile": inputs.profile,
            "scope": size,
            "route": plan.name,
            "scheduling": plan.scheduling,
            "status": "measured",
        }

    monkeypatch.setattr(comparison, "AcquireBatch", acquire)
    monkeypatch.setattr(comparison, "RunSample", replay)
    comparison.Collect(
        Namespace(
            config=Path("unused"),
            qfq_database=Path("unused"),
            profiles=("v2", "v3"),
            sources=("tencent",),
            mode="single",
            sizes=(120,),
            rounds=2,
            timeout_seconds=1,
            interval_seconds=0,
            source_loop_seconds=120,
            task_seconds=600,
        )
    )
    assert len(acquired) == 2
    assert len(replayed) == 4
    assert all(received is batch and active_references is references for received, active_references in replayed)


def test_compact_result_omits_raw_samples_and_preserves_bounded_findings() -> None:
    field_rows = [
        {
            "field": field,
            "coverage": 1.0 if field != "previous_close" else 0.0,
            "missing_or_invalid": 0 if field != "previous_close" else 1,
        }
        for field in comparison.QUOTE_FIELDS
    ]
    audit = {"requested": 1, "returned": 1, "core_complete": 1, "fields": field_rows}
    sample = {
        "profile": "v3",
        "scope": 120,
        "route": "tencent",
        "scheduling": "single",
        "sources": ["tencent"],
        "status": "measured",
        "raw_quote_complete": True,
        "quotes": audit,
        "acquisition_ms": 100,
        "market_data_ready_ms": 110,
        "input_seal_ms": 120,
        "scoring_ms": 20,
        "total_ms": 140,
        "factors": {
            "fields": [],
            "models": [],
            "qfq_required_sessions": 61,
            "qfq_complete_rows": 1,
            "model_industry_rows": 1,
        },
    }
    acquisition = {
        "scope": 120,
        "route": "tencent",
        "scheduling": "single",
        "attempts": [{"source": "tencent", "elapsed_ms": 100, "physical_rows": 1, "error": None, "audit": audit}],
    }
    summary = comparison.CompactSummary(
        samples=[sample],
        acquisitions=[acquisition],
        profiles=("v3",),
        official_population=1,
        eligible_population=1,
        eligibility={"integrity_ok": True},
        compared_sources=("tencent", "eastmoney", "sina", "baostock"),
        baostock_results=[],
        preparation=[],
        reference_setup_ms=1,
        industry_probe={},
        auxiliary_report=(),
        budget={"excluded_sources": []},
    )
    assert "samples" not in summary
    assert summary["recommended_plan"]["selected_route"] == "tencent"
    assert summary["recommended_plan"]["unresolved_source_inputs"] == []
    assert summary["recommended_plan"]["all_source_owned_inputs_complete"] is True
    assert summary["route_results"][0]["complete_scoring_input_rounds"] == 0
    assert summary["input_owners"]["required_realtime_fields"] == [
        "price",
        "pct_change",
        "amount",
        "high",
        "low",
        "turnover_rate",
        "volume",
        "amplitude",
    ]
    assert summary["input_owners"]["local_industry_fields"] == ["industry", "model_industry"]
    assert summary["input_owners"]["derivable_realtime_fields"] == []
    assert summary["input_owners"]["optional_fallback_realtime_fields"] == ["market_cap", "volume_ratio"]
    assert summary["input_owners"]["validation_realtime_fields"] == ["previous_close", "open_price"]
    assert summary["input_owners"]["ignored_realtime_fields"] == ["speed", "change_5m"]

    report = build_report(
        "market-factor-compare",
        (
            DiagnosticResult(
                "market_factor_compare",
                0,
                1,
                {
                    "schema_version": "market-factor-comparison",
                    "status": "degraded",
                    "summary": summary,
                    "findings": [{"severity": "warning", "code": "source_inputs_incomplete"}],
                },
                None,
            ),
        ),
    )
    assert report["findings"] == [
        {"severity": "warning", "code": "source_inputs_incomplete", "check": "market_factor_compare"}
    ]
    rendered = RenderMarketFactorText(report)
    assert "一、单源结果" in rendered
    assert "采集至合并校验完成" in rendered
    assert "完整评分输入=0/1轮" in rendered
    assert "诊断总耗时" in rendered
    assert '"source_results"' not in rendered


def test_route_plans_keep_daily_industry_reference_outside_quote_combinations() -> None:
    plans = comparison.RoutePlans(("tencent", "eastmoney", "sina", "baostock"), "all")
    assert {plan.name for plan in plans} == {"tencent", "eastmoney", "sina", "tencent_then_sina"}
    assert all(plan.scheduling in {"single", "sequential_failure_backup"} for plan in plans)
    assert all(plan.sources[0] == "tencent" for plan in plans if len(plan.sources) > 1)
    assert all("baostock" not in plan.sources for plan in plans)


def test_recommendation_requires_tencent_primary_and_prefers_complete_pair() -> None:
    complete_fields = {
        "complete_fields": list(comparison.QUOTE_FIELDS),
        "partial_fields": [],
        "unavailable_fields": [],
    }
    incomplete_fields = {
        "complete_fields": list(comparison.QUOTE_FIELDS[:-1]),
        "partial_fields": [],
        "unavailable_fields": [comparison.QUOTE_FIELDS[-1]],
    }
    routes = [
        {
            "route": "eastmoney",
            "scope": "full_market",
            "sources": ["eastmoney"],
            "rounds": 3,
            "measured_rounds": 3,
            "source_owned_inputs_complete_rounds": 3,
            "core_complete_rounds": 3,
            "model_industry_complete_rounds": 3,
            "scoring_market_fields_complete_rounds": 3,
            "selection_market_fields_complete_rounds": 3,
            "all_market_fields_complete_rounds": 3,
            "fields": complete_fields,
            "timings_ms": {"market_data_ready_ms": {"p95_ms": 50}},
        },
        {
            "route": "fast_incomplete",
            "scope": "full_market",
            "sources": ["tencent"],
            "rounds": 3,
            "measured_rounds": 3,
            "source_owned_inputs_complete_rounds": 0,
            "core_complete_rounds": 3,
            "model_industry_complete_rounds": 0,
            "scoring_market_fields_complete_rounds": 0,
            "selection_market_fields_complete_rounds": 0,
            "all_market_fields_complete_rounds": 0,
            "fields": incomplete_fields,
            "timings_ms": {"market_data_ready_ms": {"p95_ms": 100}},
        },
        {
            "route": "complete_pair",
            "scope": "full_market",
            "sources": ["tencent", "sina"],
            "rounds": 3,
            "measured_rounds": 3,
            "source_owned_inputs_complete_rounds": 3,
            "core_complete_rounds": 3,
            "model_industry_complete_rounds": 3,
            "scoring_market_fields_complete_rounds": 3,
            "selection_market_fields_complete_rounds": 3,
            "all_market_fields_complete_rounds": 3,
            "fields": complete_fields,
            "timings_ms": {"market_data_ready_ms": {"p95_ms": 200}},
        },
        {
            "route": "complete_triple",
            "scope": "full_market",
            "sources": ["tencent", "eastmoney", "sina"],
            "rounds": 3,
            "measured_rounds": 3,
            "source_owned_inputs_complete_rounds": 3,
            "core_complete_rounds": 3,
            "model_industry_complete_rounds": 3,
            "scoring_market_fields_complete_rounds": 3,
            "selection_market_fields_complete_rounds": 3,
            "all_market_fields_complete_rounds": 3,
            "fields": complete_fields,
            "timings_ms": {"market_data_ready_ms": {"p95_ms": 200}},
        },
    ]
    assert comparison._recommended_route(routes)["route"] == "complete_pair"
    assert comparison._recommended_route(routes, frozenset({"sina"}))["route"] == "fast_incomplete"


def test_source_loop_budget_excludes_slow_source_and_task_budget_stops_work() -> None:
    ticks = iter((0.0, 5.0, 13.0, 15.0))
    budget = DiagnosticBudget(2.0, 12.0, monotonic=lambda: next(ticks))

    budget.record_source("eastmoney", 1_250.0, "eastmoney")
    assert budget.source_remaining_seconds("eastmoney") == 0.75
    assert budget.route_allowed(("tencent", "eastmoney"))
    budget.record_source("eastmoney", 750.0, "tencent_with_eastmoney")
    assert not budget.route_allowed(("tencent", "eastmoney"))
    assert budget.route_allowed(("tencent", "sina"))
    assert not budget.task_expired(("tencent",), "tencent")
    assert budget.task_expired(("tencent",), "tencent")
    report = budget.report()
    assert report["task_timed_out"] is True
    assert [item["source"] for item in report["excluded_sources"]] == ["eastmoney", "tencent"]


def test_baostock_reports_reference_coverage_without_claiming_instant_quotes() -> None:
    reference = SimpleNamespace(effective_date=AT.date())
    capability = BaostockCapability({"600001": reference}, 1500)

    result = BaoStockSourceResults(
        capability,
        (("full_market", ("600001", "600002")),),
        3,
        comparison.QUOTE_FIELDS,
    )[0]

    assert result["coverage"] == 0.5
    assert result["network_fetch_rounds"] == 0
    assert result["cache_reuse_rounds"] == 3
    assert result["network_status"] == "not_requested"
    assert result["supported_reference_fields"] == [
        "model_industry",
        "model_industry_classification",
        "model_industry_effective_date",
        "model_industry_source",
        "model_industry_data_version",
    ]
    assert result["unsupported_instant_quote_fields"] == list(comparison.QUOTE_FIELDS)


def test_auxiliary_probe_uses_injected_pool_and_keeps_unavailable_evidence_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auxiliary.SQLiteCompanyEvidenceReader, "visit", lambda *args, **kwargs: None)
    monkeypatch.setattr(auxiliary.EastmoneyClient, "fetch_intraday_minutes", lambda *args, **kwargs: ())
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="factor-test")
    pool.start()
    try:
        result = auxiliary.ProbeAuxiliary(
            ("600001",),
            AT,
            LoadStrategySettings(Path("config/strategy.json")).long_research,
            1,
            pool,
            company_database=Path("unused/evidence.sqlite3"),
            open_dates=(AT.date(),),
        )
    finally:
        pool.stop(wait=True, cancel_futures=True)
    assert result.report[0]["financial"] is False
    assert result.report[0]["corporate_risk_window_complete"] is False
    assert result.report[1]["rows"] == 0


def test_diagnostic_eastmoney_parser_preserves_units_missing_and_source_time_uncertainty() -> None:
    from trader.infra.http_contracts import HttpResponse
    from trader.recommendation.infra.market_data.providers.eastmoney_spot import EastmoneySpotClient

    calls = []

    class Transport:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            return HttpResponse(200, b'{"data":{"diff":[],"total":0}}')

    EastmoneySpotClient(Transport()).fetch_page(1, host="push2.eastmoney.com", timeout=2, deadline=3, received_at=AT)
    requested_fields = set(calls[0]["params"]["fields"].split(","))
    assert {"f11", "f22"}.isdisjoint(requested_fields)
    assert "f100" in requested_fields
    assert "f5" in requested_fields
    quote = ParseEastmoneySpotQuote(
        {
            "f12": "600001",
            "f14": "fixture",
            "f2": 12,
            "f3": 1.5,
            "f6": 100_000_000,
            "f5": 250_000,
            "f20": 10_000_000_000,
            "f22": 0,
            "f11": "-",
            "f10": 1.2,
            "f8": 0,
        },
        AT,
    )
    assert quote is not None
    assert quote.amount == 100_000_000
    assert quote.volume == 25_000_000
    assert quote.market_cap == 10_000_000_000
    assert quote.pct_change == 1.5
    assert quote.speed == 0
    assert quote.change_5m is None
    assert quote.source_time == quote.received_time


def test_eastmoney_probe_rejects_duplicate_pages_instead_of_reporting_full_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    rows = tuple(_quote(f"{600000 + index}") for index in range(100))
    monkeypatch.setattr(eastmoney_probe, "_page", lambda *args: (rows, 200))
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="factor-test")
    pool.start()
    try:
        with pytest.raises(ValueError, match="incomplete population"):
            eastmoney_probe.FetchEastmoneyQuotes(1, time.monotonic() + 5, pool)
    finally:
        pool.stop(wait=True, cancel_futures=True)


def test_child_sdk_stdout_cannot_corrupt_json_report(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import json
    import os
    import sys

    def Collect(args, *, cpu_profile_dir=None):
        os.write(1, b"supplier login success\n")
        return {"status": "degraded", "summary": {"complete": False}}

    monkeypatch.setattr(comparison, "Collect", Collect)
    monkeypatch.setattr(sys, "argv", ["probe", "--config", "config/runtime.json", "--qfq-database", "data/qfq"])
    assert comparison.main() == 0
    output = capfd.readouterr()
    assert json.loads(output.out)["status"] == "degraded"
    assert "supplier login success" in output.err

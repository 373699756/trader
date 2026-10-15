from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics import market_factor_auxiliary as auxiliary
from scripts.runtime_diagnostics import market_factor_compare as comparison
from scripts.runtime_diagnostics import market_factor_eastmoney as eastmoney_probe
from scripts.runtime_diagnostics.market_factor_audit import factor_audit, missing_quote_codes, quote_audit
from scripts.runtime_diagnostics.market_factor_eastmoney import parse_quote
from scripts.runtime_diagnostics.market_factor_inputs import FixedReferences, history_at_cut, read_references
from trader.infra.market_data.history.history import DailyBar, PriceAdjustment
from trader.infra.settings import load_strategy_settings
from trader.infra.workers import BoundedExecutor
from trader.recommendation.domain.market.models import Board, FeatureSnapshot, MarketQuote
from trader.recommendation.domain.market.research import ResearchObservation
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


def test_coverage_keeps_missing_requested_rows_and_valid_zero_values() -> None:
    report = quote_audit((_quote(), _quote("600009")), ("600001", "600002"), AT)
    fields = {item["field"]: item for item in report["fields"]}
    assert report["returned"] == 1
    assert fields["speed"]["valid"] == 1
    assert fields["speed"]["missing_or_invalid"] == 1
    assert fields["price"]["coverage"] == 0.5
    assert report["core_complete"] == 1


def test_quote_age_never_treats_received_time_or_future_time_as_verified_freshness() -> None:
    quotes = (
        _quote(),
        _quote("600002", source="sina"),
        _quote("600003", source_time=AT),
        _quote("600004", source_time=AT + timedelta(seconds=1)),
    )
    report = quote_audit(quotes, tuple(q.code for q in quotes), AT)
    assert report["source_timestamp_available"] == 2
    assert report["fresh_source_timestamp_rows"] == 1
    assert report["future_timestamp_rows"] == 1


def test_supplementation_includes_missing_speed_and_preserves_zero() -> None:
    quotes = (_quote(speed=None), _quote("600002"))
    assert missing_quote_codes(quotes, ("600001", "600002", "600003"), AT) == ("600001", "600003")


def test_factor_audit_counts_only_requested_histories_and_derived_local_factors() -> None:
    policy = _recommendation_policy(load_strategy_settings(Path("config/strategy.json")))
    inputs = SimpleNamespace(
        profile="v3",
        policy=policy,
        histories={"600001": (1,) * 61, "600009": (1,) * 61},
        history_sessions=61,
        models=SimpleNamespace(
            history_required_sessions=lambda strategy: 61, is_input_eligible=lambda strategy, item: False
        ),
    )
    feature = FeatureSnapshot(_quote(), {"amount_median_20d": 100_000_000, "turnover_median_20d": 1}, AT)
    report = factor_audit((feature,), inputs, ("600001", "600002"))
    fields = {item["field"]: item for item in report["fields"]}
    assert report["qfq_complete_rows"] == 1
    assert report["qfq_missing_rows"] == 1
    assert report["qfq_length_distribution"] == {"61": 1, "0": 1}
    assert report["qfq_one_session_short_rows"] == 0
    assert fields["amount_shock_score"]["valid"] == 1
    assert fields["amount_shock_score"]["missing"] == 1
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
    report = comparison.summarize(samples)[0]
    assert report["measured_rounds"] == 1
    assert report["factor_complete_rounds"] == 0
    assert report["complete_input_timings_ms"]["sample_count"] == 0


def test_supplement_only_fetches_missing_codes_under_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fetch(source, codes, pool, timeout, deadline):
        calls.append((source, codes, deadline))
        return comparison.SourceAttempt(source, (_quote(speed=None), _quote("600002")), 1, 2, None)

    monkeypatch.setattr(comparison, "fetch_source", fetch)
    batch = comparison.acquire_batch(
        ("tencent", "sina"), ("600001", "600002"), None, Namespace(batch_seconds=5, timeout_seconds=1)
    )
    assert len(batch.attempts) == 2
    assert calls[1][1] == ("600001",)
    assert calls[0][2] == calls[1][2]


def test_market_factor_profile_uses_unified_output_and_explicit_bounds() -> None:
    parser = _parser()
    args = parser.parse_args(["--profile", "market-factor-compare", "--market-sizes", "120", "--market-mode", "single"])
    options, _output = _validate(args)
    command = build_commands(options)[0]
    assert command.name == "market_factor_compare"
    assert "scripts.runtime_diagnostics.market_factor_compare" in command.argv
    assert "--batch-seconds" in command.argv
    assert "--output" not in command.argv


def test_reference_probe_never_initializes_missing_runtime_database(tmp_path: Path) -> None:
    snapshot = read_references(tmp_path, AT)
    assert not snapshot.observations
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
    assert len(history_at_cut(bars, AT)) == 1
    assert len(history_at_cut(bars, AT.replace(hour=15))) == 2


def test_profiles_share_exactly_one_acquisition_per_route_and_round(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = comparison.CollectedBatch((), (), AT, 10)
    acquired = []
    replayed = []
    monkeypatch.setattr(
        comparison, "load_runtime_settings", lambda path: SimpleNamespace(runtime_dir=path, strategy_config_path=path)
    )
    monkeypatch.setattr(comparison, "read_references", lambda path, at: FixedReferences((), {}, (), ()))
    monkeypatch.setattr(comparison, "BaoStockIndustryClient", lambda **kwargs: SimpleNamespace(fetch=lambda at: ()))
    monkeypatch.setattr(
        comparison, "ExchangeSecurityMasterClient", lambda **kwargs: SimpleNamespace(fetch=lambda at: ())
    )
    monkeypatch.setattr(comparison, "load_strategy_settings", lambda path: SimpleNamespace(long_research=None))
    monkeypatch.setattr(comparison, "probe_auxiliary", lambda *args: SimpleNamespace(report=()))
    monkeypatch.setattr(
        comparison,
        "load_factor_inputs",
        lambda config, qfq, profile, codes, at: SimpleNamespace(
            profile=profile, snapshot_hash="fixed", config_version="fixed", preparation_ms={}
        ),
    )

    def acquire(*args):
        acquired.append(args)
        return batch

    def replay(route, codes, inputs, references, official, received, args, number, size, auxiliary):
        replayed.append(received)
        return {"profile": inputs.profile, "scope": size, "route": "+".join(route), "status": "measured"}

    monkeypatch.setattr(comparison, "acquire_batch", acquire)
    monkeypatch.setattr(comparison, "run_sample", replay)
    comparison.collect(
        Namespace(
            config=Path("unused"),
            qfq_root=Path("unused"),
            profiles=("v2", "v3"),
            sources=("tencent",),
            mode="single",
            sizes=(120,),
            rounds=2,
            timeout_seconds=1,
            interval_seconds=0,
        )
    )
    assert len(acquired) == 2
    assert len(replayed) == 4
    assert all(item is batch for item in replayed)


def test_auxiliary_probe_uses_injected_pool_and_keeps_unavailable_evidence_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auxiliary.AkshareResearchClient, "fetch_snapshot", lambda *args, **kwargs: ResearchObservation()
    )
    monkeypatch.setattr(auxiliary.EastmoneyClient, "fetch_intraday_minutes", lambda *args, **kwargs: ())
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="factor-test")
    pool.start()
    try:
        result = auxiliary.probe_auxiliary(
            ("600001",), AT, load_strategy_settings(Path("config/strategy.json")).long_research, 1, pool
        )
    finally:
        pool.stop(wait=True, cancel_futures=True)
    assert result.report[0]["financial"] is False
    assert result.report[0]["corporate_risk_history_complete"] is False
    assert result.report[1]["rows"] == 0


def test_diagnostic_eastmoney_parser_preserves_units_missing_and_source_time_uncertainty() -> None:
    quote = parse_quote(
        {
            "f12": "600001",
            "f14": "fixture",
            "f2": 12,
            "f3": 1.5,
            "f6": 100_000_000,
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
            eastmoney_probe.fetch_eastmoney_quotes(1, time.monotonic() + 5, pool)
    finally:
        pool.stop(wait=True, cancel_futures=True)


def test_child_sdk_stdout_cannot_corrupt_json_report(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import json
    import os
    import sys

    def collect(args):
        os.write(1, b"supplier login success\n")
        return {"status": "degraded", "summary": {"complete": False}}

    monkeypatch.setattr(comparison, "collect", collect)
    monkeypatch.setattr(sys, "argv", ["probe", "--config", "config/runtime.json", "--qfq-root", "data/qfq"])
    assert comparison.main() == 0
    output = capfd.readouterr()
    assert json.loads(output.out)["status"] == "degraded"
    assert "supplier login success" in output.err

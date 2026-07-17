from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.infrastructure.settings import (
    ConfigurationError,
    load_long_watchlist,
    load_runtime_settings,
    load_strategy_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"


def test_v2_configuration_contract_is_valid() -> None:
    runtime = load_runtime_settings(RUNTIME_CONFIG)
    strategy = load_strategy_settings(runtime.strategy_config_path)
    watchlist = load_long_watchlist(runtime.long_watchlist_path)

    assert runtime.schema_version == 2
    assert strategy.schema_version == 5
    assert runtime.runtime_dir == PROJECT_ROOT / ".runtime" / "v2"
    assert runtime.market_data.research_timeout_seconds == 8
    assert sum(runtime.deepseek.strategy_limits.values()) == 188
    assert strategy.fusion.local_weight == pytest.approx(0.68)
    assert strategy.fusion.deepseek_weight == pytest.approx(0.32)
    regulatory_rule = next(rule for rule in strategy.risk_rules if rule.risk_code == "regulatory_risk")
    assert regulatory_rule.veto is True
    assert regulatory_rule.allowed_evidence_types == ("announcement", "regulatory_filing")
    assert regulatory_rule.trigger_factor == "negative_announcement_level"
    assert regulatory_rule.trigger_thresholds == (3.0,)
    assert regulatory_rule.combination_mode == "exclusive"
    assert strategy.today_news_signal.lookback_hours == 72.0
    assert strategy.today_news_signal.freshness_full_score_hours == 1.0
    assert strategy.today_news_signal.positive_score == 75.0
    assert "回购" in strategy.today_news_signal.positive_keywords
    assert "减持" in strategy.today_news_signal.negative_keywords
    assert len(watchlist.items) == 10


def test_invalid_strategy_weight_sum_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    source = (PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8")
    strategy_path.write_text(source.replace('"local_weight": 0.68', '"local_weight": 0.5'), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fusion weights"):
        load_strategy_settings(strategy_path)


def test_alternative_fusion_weights_are_rejected_even_when_they_sum_to_one(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["fusion"]["local_weight"] = 0.5
    raw["fusion"]["deepseek_weight"] = 0.5
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 0.68 and 0.32"):
        load_strategy_settings(strategy_path)


def test_non_finite_configuration_number_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["selection"]["observation_margin"] = float("nan")
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="finite"):
        load_strategy_settings(strategy_path)


def test_incomplete_risk_trigger_contract_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    del raw["risk_rules"][0]["trigger"]
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="trigger"):
        load_strategy_settings(strategy_path)


def test_risk_rule_cannot_use_factor_outside_registered_strategy(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    high_volatility = next(rule for rule in raw["risk_rules"] if rule["risk_code"] == "high_volatility")
    high_volatility["strategies"].append("long")
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="outside its registered strategies"):
        load_strategy_settings(strategy_path)


def test_risk_identity_fields_reject_non_string_values(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["risk_rules"][0]["risk_fact_id_fields"] = ["stock_code", {"invalid": True}]
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="stable identity fields"):
        load_strategy_settings(strategy_path)


def test_today_news_signal_rejects_overlapping_keyword_sets(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["today_news_signal"]["negative_keywords"].append(raw["today_news_signal"]["positive_keywords"][0])
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="must not overlap"):
        load_strategy_settings(strategy_path)


def test_today_news_signal_changes_strategy_version(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    baseline = load_strategy_settings(source)
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["today_news_signal"]["positive_keywords"].append("订单增长")
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    changed = load_strategy_settings(changed_path)

    assert changed.strategy_version != baseline.strategy_version


def test_today_news_signal_is_required(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    del raw["today_news_signal"]
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="today_news_signal must be an object"):
        load_strategy_settings(changed_path)


def test_today_news_signal_fixed_window_and_scores_cannot_drift(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "v2" / "strategy.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["today_news_signal"]["lookback_hours"] = 48
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 72h/1h and 75/50/25"):
        load_strategy_settings(changed_path)

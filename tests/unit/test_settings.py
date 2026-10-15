from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.infra.settings import (
    ConfigurationError,
    LoadLongWatchlist,
    LoadRuntimeSettings,
    LoadStrategySettings,
)
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.scoring import (
    BoardCandidateComponents,
    BoardCandidateScore,
    ScoreBoardStrategy,
)
from trader.recommendation.infra.normalization.features import FEATURE_SCHEMA_ID, FEATURE_SCHEMA_NAMES
from trader.recommendation.infra.policy_settings import _recommendation_policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = PROJECT_ROOT / "config" / "runtime.json"

_RUNTIME_FILES = {
    "delivery.json": ("server", "api"),
    "scheduler.json": ("pipeline",),
    "market_data.json": ("market_data",),
    "cache.json": ("cache_policy",),
    "performance.json": ("performance_budgets",),
    "deepseek.json": ("deepseek",),
}
_STRATEGY_FILES = {
    "selection.json": ("selection",),
    "filter.json": ("hard_filters",),
    "candidate.json": ("board_candidate_weights", "candidate_component_weights"),
    "scoring.json": (
        "fusion",
        "board_local_strategy_weights",
        "local_component_weights",
        "feature_component_weights",
        "tomorrow_tail_signal",
        "market_regime",
        "dimension_weights",
    ),
    "risk.json": ("risk_rules",),
    "features.json": ("model_contracts", "factor_index", "factor_contract", "news_signal"),
    "long.json": ("long_research",),
}


def _merged_config(root_name: str, section_files: dict[str, tuple[str, ...]]) -> dict[str, object]:
    config_dir = PROJECT_ROOT / "config"
    result = json.loads((config_dir / root_name).read_text(encoding="utf-8"))
    for filename in section_files:
        result.update(json.loads((config_dir / filename).read_text(encoding="utf-8")))
    return result


def _write_split_config(
    tmp_path: Path,
    root_name: str,
    raw: dict[str, object],
    section_files: dict[str, tuple[str, ...]],
) -> Path:
    config_dir = tmp_path / "config"
    if not config_dir.exists():
        shutil.copytree(PROJECT_ROOT / "config", config_dir)
    owned = {key for keys in section_files.values() for key in keys}
    (config_dir / root_name).write_text(
        json.dumps({key: value for key, value in raw.items() if key not in owned}),
        encoding="utf-8",
    )
    for filename, keys in section_files.items():
        (config_dir / filename).write_text(
            json.dumps({key: raw[key] for key in keys if key in raw}),
            encoding="utf-8",
        )
    return config_dir / root_name


def _runtime_document() -> dict[str, object]:
    raw = _merged_config("runtime.json", _RUNTIME_FILES)
    market = raw["market_data"]
    assert isinstance(market, dict)
    market["cache_policy"] = raw.pop("cache_policy")
    return raw


def _write_runtime_config(tmp_path: Path, raw: dict[str, object]) -> Path:
    market = raw.get("market_data")
    if isinstance(market, dict) and "cache_policy" in market:
        raw = dict(raw)
        market = dict(market)
        raw["market_data"] = market
        raw["cache_policy"] = market.pop("cache_policy")
    return _write_split_config(tmp_path, "runtime.json", raw, _RUNTIME_FILES)


def _strategy_document() -> dict[str, object]:
    return _merged_config("strategy.json", _STRATEGY_FILES)


def _write_strategy_config(tmp_path: Path, raw: dict[str, object]) -> Path:
    return _write_split_config(tmp_path, "strategy.json", raw, _STRATEGY_FILES)


def test_configuration_contract_is_valid() -> None:
    runtime = LoadRuntimeSettings(RUNTIME_CONFIG)
    strategy = LoadStrategySettings(runtime.strategy_config_path)
    watchlist = LoadLongWatchlist(runtime.long_watchlist_path)

    assert strategy.scoring_profile == "v2"
    assert runtime.config_version.startswith("runtime_sha256_")
    assert strategy.strategy_version.startswith("strategy_sha256_")
    assert runtime.market_data.source_contracts["eastmoney"] == ("eastmoney_quote_security_master")
    assert runtime.api.default_top_n == 12
    assert runtime.api.maximum_top_n == 12
    assert runtime.runtime_dir == PROJECT_ROOT / ".runtime" / "trader"
    assert runtime.freeze_dir == PROJECT_ROOT / "data" / "freeze"
    assert runtime.market_data.research_timeout_seconds == 8
    assert runtime.market_data.eastmoney_timeout_seconds == 8
    assert runtime.market_data.sina_timeout_seconds == 8
    assert runtime.market_data.full_market_hedge_delay_seconds == 1
    assert runtime.pipeline.market_workers == 5
    assert runtime.api.web_snapshot_retention_seconds == 35
    assert runtime.market_data.tushare.timeout_seconds == 8
    assert runtime.market_data.tushare.points == 120
    assert runtime.market_data.source_contracts["tushare"] == "tushare_sdk_120_point_daily_audit"
    assert runtime.market_data.tushare.token_file == PROJECT_ROOT / ".token_key"
    assert set(runtime.market_data.cache_policy.datasets) == {
        "full_market_quotes",
        "candidate_quotes",
        "intraday_minutes",
        "research_success",
        "research_failure",
        "security_master_calendar",
        "daily_valuation_financials",
        "canonical_market_snapshot",
        "canonical_candidate_snapshot",
        "current_quote_index",
        "candidate_feature_batch",
        "hard_filter_batch",
        "board_cross_section",
        "candidate_preselection",
        "board_score_batch",
        "global_local_draft",
        "competition_group_mapping",
        "raw_deepseek_review",
        "strategy_deepseek_review",
        "deepseek_seen_codes",
        "published_recommendation_view",
        "published_date_index",
    }
    assert runtime.market_data.cache_policy.estimator == "canonical_json_utf8"
    assert runtime.market_data.cache_policy.total_bytes == 248 * 1024 * 1024
    assert runtime.market_data.cache_policy.runtime_reserve_bytes == 8 * 1024 * 1024
    assert runtime.market_data.cache_policy.pool_total_bytes == 256 * 1024 * 1024
    assert runtime.market_data.cache_policy.datasets["candidate_quotes"].capacity == 1080
    assert {name: policy.persisted for name, policy in runtime.market_data.cache_policy.datasets.items()} == {
        "full_market_quotes": False,
        "candidate_quotes": False,
        "intraday_minutes": False,
        "research_success": True,
        "research_failure": False,
        "security_master_calendar": False,
        "daily_valuation_financials": False,
        "canonical_market_snapshot": False,
        "canonical_candidate_snapshot": False,
        "current_quote_index": False,
        "candidate_feature_batch": False,
        "hard_filter_batch": False,
        "board_cross_section": False,
        "candidate_preselection": False,
        "board_score_batch": False,
        "global_local_draft": False,
        "competition_group_mapping": False,
        "raw_deepseek_review": False,
        "strategy_deepseek_review": False,
        "deepseek_seen_codes": False,
        "published_recommendation_view": False,
        "published_date_index": False,
    }
    assert runtime.performance_budgets.workload.market_rows == 5500
    assert runtime.performance_budgets.workload.candidate_rows == 1080
    assert runtime.performance_budgets.rounds.warmup == 1
    assert runtime.performance_budgets.rounds.measurement == 5
    assert runtime.performance_budgets.latency_p95_ms["market_normalization"] == 250
    assert runtime.performance_budgets.latency_p95_ms["board_preselection"] == 5000
    assert runtime.performance_budgets.latency_p95_ms["market_merge"] == 600
    assert runtime.performance_budgets.latency_p95_ms["canonical_snapshot"] == 900
    assert runtime.performance_budgets.latency_p95_ms["targeted_overlay_commit"] == 100
    assert runtime.performance_budgets.latency_p95_ms["quote_to_draft"] == 5000
    assert runtime.performance_budgets.latency_p95_ms["browser_patch_to_paint"] == 100
    assert runtime.performance_budgets.memory.cache_logical_bytes == 260046848
    assert runtime.performance_budgets.memory.process_peak_rss_bytes == 402653184
    assert set(runtime.pipeline.cadence_seconds["full_market"].values()) == {10.0}
    assert runtime.pipeline.cadence_seconds["candidate_quotes"]["morning_main"] == 1
    assert runtime.pipeline.cadence_seconds["candidate_quotes"]["final_window"] == 1
    assert runtime.pipeline.cadence_seconds["topk_quotes"]["morning_main"] == 1
    assert runtime.performance_budgets.data_age_p95_seconds["full_market_main"] == 10
    assert runtime.market_data.circuit_breaker_seconds == 30
    assert runtime.deepseek.daily_hard_limit == 168
    assert runtime.deepseek.strategy_limits == {
        "tomorrow": 38,
        "d25": 16,
        "shared_preheat": 4,
        "emergency": 5,
    }
    assert sum(runtime.deepseek.strategy_limits.values()) == 63
    assert sum(runtime.deepseek.stage_targets.values()) == 31
    assert sum(runtime.deepseek.stage_limits.values()) == 63
    assert sum(limit for stage, limit in runtime.deepseek.stage_limits.items() if stage != "emergency") == 58
    assert runtime.deepseek.timeout_seconds == 20
    assert runtime.deepseek.batch_size == 4
    assert runtime.deepseek.model == "deepseek-v4-flash"
    assert runtime.deepseek.challenger_model == "deepseek-v4-pro"
    assert runtime.deepseek.challenger_limits == {"tomorrow": 2, "d25": 0}
    assert runtime.deepseek.challenger_daily_limit == 2
    assert runtime.deepseek.adaptive.cooldown_seconds == 900
    assert runtime.deepseek.adaptive.minimum_application_ratio == pytest.approx(0.4)
    assert strategy.hard_filters.blacklist_codes == ()
    assert strategy.hard_filters.structured_risk_thresholds == {
        "major_shareholder_reduction": 0.0,
        "financial_fraud_history": 0.0,
        "official_investigation_history": 0.0,
        "major_illegal_history": 0.0,
        "fund_occupation_history": 0.0,
        "illegal_guarantee_history": 0.0,
        "forced_delisting_risk": 0.0,
        "unlock_risk": 0.0,
        "pledge_risk": 0.0,
        "financial_deterioration": 0.5,
    }
    assert strategy.fusion.version == "local_score_structured_review"
    assert strategy.fusion.local_risk_cap == pytest.approx(25.0)
    regulatory_rule = next(rule for rule in strategy.risk_rules if rule.risk_code == "regulatory_risk")
    assert regulatory_rule.veto is False
    assert regulatory_rule.allowed_evidence_types == ("announcement", "regulatory_filing")
    assert regulatory_rule.trigger_factor == "negative_announcement_level"
    assert regulatory_rule.trigger_thresholds == (3.0,)
    assert regulatory_rule.combination_mode == "exclusive"
    assert strategy.news_signal.lookback_hours == 72.0
    assert strategy.news_signal.freshness_full_score_hours == 1.0
    assert strategy.news_signal.positive_score == 75.0
    assert "回购" in strategy.news_signal.positive_keywords
    assert "减持" in strategy.news_signal.negative_keywords
    assert strategy.tomorrow_tail_signal.lookback_minutes == 30
    assert strategy.tomorrow_tail_signal.minimum_baseline_minutes == 30
    assert strategy.tomorrow_tail_signal.return_score_points_per_pct == 25.0
    assert strategy.tomorrow_tail_signal.volume_score_points_per_ratio == 50.0
    assert strategy.market_regime.risk_on_breadth_min == 60.0
    assert strategy.market_regime.risk_off_breadth_max == 40.0
    assert strategy.selection.review_candidate_limit == 28
    assert strategy.selection.default_top_k == 6
    assert strategy.selection.maximum_top_k == 12
    assert set(strategy.dimension_weights) == {"tomorrow", "d25"}
    assert strategy.long_research.financial_max_age_days == 550
    assert strategy.long_research.pledge_thresholds == (10.0, 20.0, 35.0)
    assert "监管函" in strategy.long_research.negative_medium_keywords
    assert watchlist.watchlist_version.startswith("watchlist_sha256_")
    assert len(watchlist.items) == 153
    assert len(watchlist.groups) == 50
    assert {group.category for group in watchlist.groups} == {
        "chokepoint",
        "future_growth",
        "low_price_potential",
    }
    assert {item.code for item in watchlist.items} == {code for group in watchlist.groups for code in group.codes}
    assert "688502" not in {item.code for item in watchlist.items}


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_scoring_profile_is_an_explicit_versioned_switch(tmp_path, profile: str) -> None:
    raw = _strategy_document()
    raw["scoring_profile"] = profile
    strategy_path = _write_strategy_config(tmp_path, raw)

    settings = LoadStrategySettings(strategy_path)

    assert settings.scoring_profile == profile
    if profile != "v2":
        assert (
            settings.strategy_version
            != LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json").strategy_version
        )


def test_scoring_profile_override_changes_the_effective_version_without_writing_config() -> None:
    source = PROJECT_ROOT / "config" / "strategy.json"
    original = source.read_bytes()

    default = LoadStrategySettings(source)
    overridden = LoadStrategySettings(source, scoring_profile="v3")

    assert default.scoring_profile == "v2"
    assert overridden.scoring_profile == "v3"
    assert overridden.strategy_version != default.strategy_version
    assert source.read_bytes() == original


def test_unknown_scoring_profile_is_rejected(tmp_path) -> None:
    raw = _strategy_document()
    raw["scoring_profile"] = "latest"
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="scoring_profile"):
        LoadStrategySettings(strategy_path)


@pytest.mark.parametrize("retired_profile", ("p1", "p2"))
def test_retired_scoring_profile_names_are_rejected(tmp_path, retired_profile: str) -> None:
    raw = _strategy_document()
    raw["scoring_profile"] = retired_profile
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="scoring_profile"):
        LoadStrategySettings(strategy_path)


def test_retired_tomorrow_specific_profile_key_is_rejected(tmp_path: Path) -> None:
    raw = _strategy_document()
    raw["tomorrow_scoring_profile"] = raw.pop("scoring_profile")
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="scoring_profile is required"):
        LoadStrategySettings(strategy_path)


@pytest.mark.parametrize("controller", ("schema_version", "config_version"))
def test_runtime_rejects_manual_version_controllers(tmp_path, controller: str) -> None:
    raw = _runtime_document()
    raw[controller] = "unsupported"
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="runtime contains unknown keys"):
        LoadRuntimeSettings(changed_path)


def test_runtime_schema_rejects_paid_full_market_sources(tmp_path) -> None:
    raw = _runtime_document()
    raw["market_data"]["full_market_sources"] = ["eastmoney", "paid_vendor"]
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="unknown keys"):
        LoadRuntimeSettings(changed_path)


def test_runtime_rejects_non_positive_web_snapshot_retention(tmp_path) -> None:
    raw = _runtime_document()
    raw["api"]["web_snapshot_retention_seconds"] = 0
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="web_snapshot_retention_seconds"):
        LoadRuntimeSettings(changed_path)


def _long_watchlist_config(groups: list[tuple[str, list[str]]]) -> dict[str, object]:
    codes = tuple(code for _category, group_codes in groups for code in group_codes)
    return {
        "items": [
            {"code": code, "name": f"测试{code}", "industry": "测试行业", "target_price": None}
            for code in dict.fromkeys(codes)
        ],
        "groups": [
            {
                "name": f"测试组{index}",
                "category": category,
                "codes": group_codes,
                "sections": [{"source_section": "current_leaders", "codes": group_codes}],
            }
            for index, (category, group_codes) in enumerate(groups)
        ],
    }


def test_long_watchlist_group_limits_are_enforced(tmp_path) -> None:
    raw = _long_watchlist_config(
        [
            ("low_price_potential", ["000001"] * 9),
            ("low_price_potential", ["000002"]),
        ]
    )
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot contain more than 8 codes"):
        LoadLongWatchlist(changed_path)


def test_long_watchlist_low_price_groups_reject_duplicate_codes(tmp_path) -> None:
    raw = _long_watchlist_config(
        [
            ("low_price_potential", ["000001"]),
            ("low_price_potential", ["000002"]),
        ]
    )
    low_price_groups = [group for group in raw["groups"] if group["category"] == "low_price_potential"]
    low_price_groups[1]["codes"][0] = low_price_groups[0]["codes"][0]
    low_price_groups[1]["sections"][0]["codes"][0] = low_price_groups[0]["codes"][0]
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot contain duplicate codes"):
        LoadLongWatchlist(changed_path)


def test_long_watchlist_rejects_codes_repeated_across_any_groups(tmp_path) -> None:
    raw = _long_watchlist_config(
        [
            ("low_price_potential", ["000001"]),
            ("low_price_potential", ["000002"]),
            ("chokepoint", ["000003"]),
        ]
    )
    raw["groups"][2]["codes"][0] = raw["groups"][0]["codes"][0]
    raw["groups"][2]["sections"][0]["codes"][0] = raw["groups"][0]["codes"][0]
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot repeat across groups"):
        LoadLongWatchlist(changed_path)


def test_long_watchlist_rejects_unknown_source_section(tmp_path) -> None:
    raw = _long_watchlist_config(
        [
            ("low_price_potential", ["000001"]),
            ("low_price_potential", ["000002"]),
        ]
    )
    raw["groups"][0]["source_section"] = "ad_hoc"
    changed_path = tmp_path / "long_watchlist.json"
    changed_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="source_section"):
        LoadLongWatchlist(changed_path)


def test_runtime_rejects_removed_decision_execution_mode(tmp_path) -> None:
    raw = _runtime_document()
    raw["pipeline"]["decision_execution_mode"] = "unsafe_parallel"
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="unknown keys"):
        LoadRuntimeSettings(changed_path)


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "unknown-model"])
def test_runtime_settings_rejects_unapproved_primary_model(tmp_path, model: str) -> None:
    raw = _runtime_document()
    raw["deepseek"]["model"] = model
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="primary model"):
        LoadRuntimeSettings(changed_path)


def test_runtime_settings_loads_deepseek_key_from_protected_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("DEEPSEEK_API_KEY=secret-from-file\n", encoding="utf-8")
    key_file.chmod(0o600)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(key_file))

    runtime = LoadRuntimeSettings(RUNTIME_CONFIG)

    assert runtime.deepseek.api_key == "secret-from-file"


def test_runtime_settings_loads_both_credentials_from_one_protected_assignment_file(tmp_path, monkeypatch) -> None:
    credential_file = tmp_path / ".token_key"
    credential_file.write_text(
        "DEEPSEEK_API_KEY=deepseek-from-file\nTUSHARE_TOKEN=tushare-from-file\n",
        encoding="utf-8",
    )
    credential_file.chmod(0o600)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(credential_file))
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(credential_file))

    runtime = LoadRuntimeSettings(RUNTIME_CONFIG)

    assert runtime.deepseek.api_key == "deepseek-from-file"
    assert runtime.market_data.tushare.token == "tushare-from-file"


def test_runtime_settings_prefers_deepseek_environment_key(tmp_path, monkeypatch) -> None:
    missing_file = tmp_path / "missing.key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-from-environment")
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(missing_file))

    runtime = LoadRuntimeSettings(RUNTIME_CONFIG)

    assert runtime.deepseek.api_key == "secret-from-environment"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_runtime_settings_rejects_insecure_deepseek_key_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("secret-from-file\n", encoding="utf-8")
    key_file.chmod(0o644)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(key_file))

    with pytest.raises(ConfigurationError, match="must not be accessible by group or other users"):
        LoadRuntimeSettings(RUNTIME_CONFIG)


def test_runtime_settings_loads_tushare_token_with_environment_priority(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "tushare.token"
    token_file.write_text("file-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("TUSHARE_TOKEN", "environment-token")
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(token_file))

    runtime = LoadRuntimeSettings(RUNTIME_CONFIG)

    assert runtime.market_data.tushare.token == "environment-token"


def test_runtime_settings_loads_tushare_token_from_configured_protected_file(tmp_path, monkeypatch) -> None:
    raw = _runtime_document()
    token_file = tmp_path / "tushare.token"
    token_file.write_text("configured-file-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    raw["market_data"]["tushare"]["token_file"] = str(token_file)
    changed_path = _write_runtime_config(tmp_path, raw)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN_FILE", raising=False)

    runtime = LoadRuntimeSettings(changed_path)

    assert runtime.market_data.tushare.token == "configured-file-token"
    assert runtime.market_data.tushare.token_file == token_file


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_runtime_settings_rejects_insecure_tushare_token_file(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "tushare.token"
    token_file.write_text("file-token\n", encoding="utf-8")
    token_file.chmod(0o644)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(token_file))

    with pytest.raises(ConfigurationError, match="Tushare token file must not be accessible"):
        LoadRuntimeSettings(RUNTIME_CONFIG)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw["market_data"]["cache_policy"].pop("groups"), "cache_policy.groups"),
        (
            lambda raw: raw["market_data"]["cache_policy"].update({"unknown": True}),
            "cache_policy contains unknown keys",
        ),
        (
            lambda raw: raw["market_data"]["cache_policy"].update({"policy_version": "unknown"}),
            "cache_policy contains unknown keys",
        ),
        (
            lambda raw: raw["market_data"]["cache_policy"].update({"estimator": "unknown"}),
            "estimator must be canonical_json_utf8",
        ),
        (
            lambda raw: raw["performance_budgets"]["workload"].update({"market_rows": 5499}),
            "performance workload",
        ),
        (
            lambda raw: raw["performance_budgets"]["workload"].update({"candidate_rows": 1079}),
            "performance workload",
        ),
        (
            lambda raw: raw["performance_budgets"]["memory"].update({"cache_total_bytes": 268435456}),
            "performance_budgets.memory contains unknown keys",
        ),
        (
            lambda raw: raw["performance_budgets"]["memory"].update({"cache_logical_bytes": 268435456}),
            "fixed 248/384 MiB",
        ),
        (
            lambda raw: raw["market_data"].update({"single_flight": False}),
            "single_flight must remain enabled",
        ),
    ],
)
def test_current_cache_and_performance_configuration_rejects_missing_unknown_or_drift(
    tmp_path, mutate, message
) -> None:
    raw = _runtime_document()
    mutate(raw)
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match=message):
        LoadRuntimeSettings(changed_path)


def test_feature_schema_contract_can_be_explicitly_reconciled_with_registered_schema(tmp_path) -> None:
    raw = _strategy_document()
    raw["factor_contract"]["feature_names"] = list(FEATURE_SCHEMA_NAMES)
    raw["factor_contract"]["feature_schema_expected"] = len(FEATURE_SCHEMA_NAMES)
    strategy_path = _write_strategy_config(tmp_path, raw)

    strategy = LoadStrategySettings(strategy_path)

    assert strategy.factor_contract["feature_schema"] == FEATURE_SCHEMA_ID
    assert strategy.factor_contract["feature_schema_expected"] == len(FEATURE_SCHEMA_NAMES)


def test_feature_schema_contract_rejects_schema_contract_version_mismatch(tmp_path) -> None:
    raw = _strategy_document()
    raw["factor_contract"]["feature_schema"] = "feature_schema_unsupported"
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="feature_schema mismatch"):
        LoadStrategySettings(strategy_path)


def test_research_timeout_cannot_exceed_point_in_time_source_limit(tmp_path) -> None:
    raw = _runtime_document()
    raw["market_data"]["research_timeout_seconds"] = 8.01
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="research_timeout_seconds must be at most 8.0"):
        LoadRuntimeSettings(changed_path)


def test_priority_reserve_must_leave_capacity_for_normal_events(tmp_path) -> None:
    raw = _runtime_document()
    raw["pipeline"]["priority_queue_size"] = raw["pipeline"]["event_queue_size"]
    changed_path = _write_runtime_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="priority_queue_size must be smaller"):
        LoadRuntimeSettings(changed_path)


def test_removed_fusion_weight_is_rejected(tmp_path) -> None:
    raw = _strategy_document()
    raw["fusion"]["local_weight"] = 0.5
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="strategy.fusion"):
        LoadStrategySettings(strategy_path)


def test_deepseek_weight_cannot_reenter_production_configuration(tmp_path) -> None:
    raw = _strategy_document()
    raw["fusion"]["deepseek_weight"] = 0.5
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="strategy.fusion"):
        LoadStrategySettings(strategy_path)


def test_non_finite_configuration_number_is_rejected(tmp_path) -> None:
    raw = _strategy_document()
    raw["selection"]["observation_margin"] = float("nan")
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="finite"):
        LoadStrategySettings(strategy_path)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    (
        ("fusion", "confidence_coverage_min", 0.1, "coverage and known-dimension"),
        ("fusion", "minimum_known_dimensions", 1, "coverage and known-dimension"),
        ("selection", "observation_margin", 6, "observation margin"),
    ),
)
def test_fixed_review_and_observation_gates_cannot_drift(
    tmp_path,
    section: str,
    field: str,
    value: float,
    message: str,
) -> None:
    raw = _strategy_document()
    raw[section][field] = value
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match=message):
        LoadStrategySettings(strategy_path)


@pytest.mark.parametrize("weight_family", ("dimension", "board_candidate", "board_local"))
def test_strategy_weight_vectors_are_loaded_from_configuration(tmp_path, weight_family: str) -> None:
    raw = _strategy_document()
    if weight_family == "dimension":
        weights = raw["dimension_weights"]["tomorrow"]
        first, second = "value_quality", "financial_health"
    elif weight_family == "board_candidate":
        weights = raw["board_candidate_weights"]["tomorrow"]["main"]
        first, second = "liquidity", "trend"
    else:
        weights = raw["board_local_strategy_weights"]["tomorrow"]["main"]
        first, second = "trend", "stability"
    weights[first] += 0.01
    weights[second] -= 0.01
    strategy_path = _write_strategy_config(tmp_path, raw)

    settings = LoadStrategySettings(strategy_path)

    if weight_family == "dimension":
        actual = settings.dimension_weights["tomorrow"]
    elif weight_family == "board_candidate":
        actual = settings.board_candidate_weights["tomorrow"]["main"]
    else:
        actual = settings.board_local_strategy_weights["tomorrow"]["main"]
    assert actual[first] == pytest.approx(weights[first])
    assert actual[second] == pytest.approx(weights[second])


def test_strategy_config_has_only_board_specific_candidate_weights() -> None:
    raw = _strategy_document()
    settings = LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json")

    assert "candidate_weights" not in raw
    assert not hasattr(settings, "candidate_weights")
    assert set(settings.board_candidate_weights) == {"tomorrow", "d25"}


def test_strategy_config_requires_complete_component_weight_families(tmp_path) -> None:
    raw = _strategy_document()

    assert set(raw["candidate_component_weights"]) == {"tomorrow", "d25"}
    assert set(raw["local_component_weights"]) == {"tomorrow", "d25"}
    assert set(raw["feature_component_weights"]) == {
        "trend_score",
        "industry_policy_score",
        "risk_protection_score",
    }

    del raw["local_component_weights"]["tomorrow"]["trend"]["breakout_20d"]
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="local_component_weights.tomorrow.trend"):
        LoadStrategySettings(strategy_path)


def test_component_weight_sum_is_rejected(tmp_path) -> None:
    raw = _strategy_document()
    raw["candidate_component_weights"]["tomorrow"]["stability"]["low_volatility_score"] = 0.6
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="candidate_component_weights.tomorrow.stability"):
        LoadStrategySettings(strategy_path)


def test_feature_component_weight_fields_and_sum_are_rejected(tmp_path) -> None:
    raw = _strategy_document()
    raw["feature_component_weights"]["trend_score"] = {"ma20_60_position": 1.0}
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="feature_component_weights.trend_score"):
        LoadStrategySettings(strategy_path)

    raw["feature_component_weights"]["trend_score"] = {
        "ma20_60_position": 0.7,
        "ma_slope": 0.4,
    }
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="feature_component_weights trend_score weights must sum"):
        LoadStrategySettings(strategy_path)


def test_feature_component_weight_configuration_is_typed_immutable_and_changes_identity(tmp_path) -> None:
    source_path = PROJECT_ROOT / "config" / "strategy.json"
    baseline = LoadStrategySettings(source_path)
    raw = _strategy_document()
    raw["feature_component_weights"]["trend_score"] = {
        "ma20_60_position": 0.7,
        "ma_slope": 0.3,
    }
    changed_path = _write_strategy_config(tmp_path, raw)

    changed = LoadStrategySettings(changed_path)

    assert changed.strategy_version != baseline.strategy_version
    assert changed.feature_component_weights.trend_score == {
        "ma20_60_position": 0.7,
        "ma_slope": 0.3,
    }
    with pytest.raises(TypeError):
        changed.feature_component_weights.trend_score["ma_slope"] = 0.4  # type: ignore[index]


def test_component_weight_configuration_reaches_domain_and_changes_strategy_identity(
    tmp_path,
    application_feature_factory,
) -> None:
    source_path = PROJECT_ROOT / "config" / "strategy.json"
    source = LoadStrategySettings(source_path)
    raw = _strategy_document()
    weights = raw["candidate_component_weights"]["tomorrow"]["stability"]
    weights["low_volatility_score"] -= 0.1
    weights["low_drawdown_score"] += 0.1
    changed_path = _write_strategy_config(tmp_path, raw)
    changed = LoadStrategySettings(changed_path)
    observed_at = datetime(2026, 9, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    feature = application_feature_factory("600001", observed_at)
    values = dict(feature.values)
    values["low_volatility_score"] = 80.0
    values["low_drawdown_score"] = 60.0
    feature = replace(feature, quote=replace(feature.quote, board=Board.MAIN), values=values)
    source_policy = _recommendation_policy(source).board_policy(Strategy.TOMORROW, Board.MAIN)
    changed_policy = _recommendation_policy(changed).board_policy(Strategy.TOMORROW, Board.MAIN)

    assert source_policy is not None
    assert changed_policy is not None
    assert source.strategy_version != changed.strategy_version
    assert changed_policy.candidate_component_weights["stability"] == weights
    assert BoardCandidateComponents(feature, source_policy) != BoardCandidateComponents(feature, changed_policy)


def test_configured_candidate_and_local_weights_preserve_two_strategy_golden_vectors(
    application_feature_factory,
) -> None:
    settings = LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json")
    policy = _recommendation_policy(settings)
    observed_at = datetime(2026, 9, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = application_feature_factory("600001", observed_at)
    feature = replace(
        source,
        quote=replace(source.quote, board=Board.MAIN),
        values={
            **source.values,
            "turnover_shock_score": 80.0,
            "amount_shock_score": 60.0,
            "flow_confirmation_score": 70.0,
        },
    )
    expected = {
        Strategy.TOMORROW: (75.58823529411764, 68.20833333333334),
        Strategy.D25: (74.58333333333333, 70.55363321799307),
    }

    for strategy, (candidate_score, local_score) in expected.items():
        board_policy = policy.board_policy(strategy, Board.MAIN)
        assert board_policy is not None
        assert BoardCandidateScore(feature, board_policy) == pytest.approx(candidate_score)
        assert ScoreBoardStrategy(feature, board_policy).base_score == pytest.approx(local_score)


def test_deepseek_risk_mapping_version_controller_is_rejected(tmp_path) -> None:
    raw = _strategy_document()
    raw["deepseek_risk_mapping_version"] = "unsupported"
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="strategy contains unknown keys"):
        LoadStrategySettings(strategy_path)


def test_incomplete_risk_trigger_contract_is_rejected(tmp_path) -> None:
    raw = _strategy_document()
    del raw["risk_rules"][0]["trigger"]
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="trigger"):
        LoadStrategySettings(strategy_path)


def test_risk_rule_cannot_use_factor_outside_registered_strategy(tmp_path) -> None:
    raw = _strategy_document()
    near_limit = next(rule for rule in raw["risk_rules"] if rule["risk_code"] == "near_limit_crowding")
    near_limit["strategies"].append("long")
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="outside its registered strategies"):
        LoadStrategySettings(strategy_path)


def test_risk_identity_fields_reject_non_string_values(tmp_path) -> None:
    raw = _strategy_document()
    raw["risk_rules"][0]["risk_fact_id_fields"] = ["stock_code", {"invalid": True}]
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="stable identity fields"):
        LoadStrategySettings(strategy_path)


def test_news_signal_rejects_overlapping_keyword_sets(tmp_path) -> None:
    raw = _strategy_document()
    raw["news_signal"]["negative_keywords"].append(raw["news_signal"]["positive_keywords"][0])
    strategy_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="must not overlap"):
        LoadStrategySettings(strategy_path)


def test_news_signal_changes_strategy_version(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "strategy.json"
    baseline = LoadStrategySettings(source)
    raw = _strategy_document()
    raw["news_signal"]["positive_keywords"].append("订单增长")
    changed_path = _write_strategy_config(tmp_path, raw)

    changed = LoadStrategySettings(changed_path)

    assert changed.strategy_version != baseline.strategy_version


def test_news_signal_is_required(tmp_path) -> None:
    raw = _strategy_document()
    del raw["news_signal"]
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="features.news_signal is required"):
        LoadStrategySettings(changed_path)


def test_news_signal_fixed_window_and_scores_cannot_drift(tmp_path) -> None:
    raw = _strategy_document()
    raw["news_signal"]["lookback_hours"] = 48
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="fixed at 72h/1h and 75/50/25"):
        LoadStrategySettings(changed_path)


def test_tomorrow_tail_signal_is_required(tmp_path) -> None:
    raw = _strategy_document()
    del raw["tomorrow_tail_signal"]
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="scoring.tomorrow_tail_signal is required"):
        LoadStrategySettings(changed_path)


def test_tomorrow_tail_signal_fixed_formula_cannot_drift(tmp_path) -> None:
    raw = _strategy_document()
    raw["tomorrow_tail_signal"]["lookback_minutes"] = 20
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="fixed at 30/30/25/50"):
        LoadStrategySettings(changed_path)


def test_tomorrow_tail_signal_rejects_manual_factor_version(tmp_path) -> None:
    raw = _strategy_document()
    raw["tomorrow_tail_signal"]["volume_score_points_per_ratio"] = 49
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="fixed at 30/30/25/50"):
        LoadStrategySettings(changed_path)
    raw["tomorrow_tail_signal"]["volume_score_points_per_ratio"] = 50
    raw["factor_index"]["tail_volume_ratio"]["version"] = "3"
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="contains unknown keys: version"):
        LoadStrategySettings(changed_path)


def test_tomorrow_tail_factor_index_cannot_contradict_executable_formula(tmp_path) -> None:
    raw = _strategy_document()
    raw["factor_index"]["tail_return_30m"]["formula"] = "clamp(50+tail_return_30m_pct*10)"
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="tail_return_30m.formula"):
        LoadStrategySettings(changed_path)


def test_d25_market_regime_policy_is_required_and_cannot_drift(tmp_path) -> None:
    raw = _strategy_document()
    del raw["market_regime"]["risk_on_breadth_min"]
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="risk_on_breadth_min"):
        LoadStrategySettings(changed_path)

    raw = _strategy_document()
    raw["market_regime"]["risk_on_breadth_min"] = 61
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="market regime boundaries are fixed"):
        LoadStrategySettings(changed_path)


def test_long_research_contract_is_required_and_hashes_keyword_changes(tmp_path) -> None:
    source = PROJECT_ROOT / "config" / "strategy.json"
    baseline = LoadStrategySettings(source)
    raw = _strategy_document()
    raw["long_research"]["announcements"]["policy_positive_keywords"].append("设备更新")
    changed_path = _write_strategy_config(tmp_path, raw)

    changed = LoadStrategySettings(changed_path)

    assert changed.strategy_version != baseline.strategy_version

    del raw["long_research"]
    changed_path = _write_strategy_config(tmp_path, raw)
    with pytest.raises(ConfigurationError, match="long.long_research is required"):
        LoadStrategySettings(changed_path)


def test_long_research_severity_keyword_levels_cannot_overlap(tmp_path) -> None:
    raw = _strategy_document()
    duplicate = raw["long_research"]["announcements"]["negative_high_keywords"][0]
    raw["long_research"]["announcements"]["negative_medium_keywords"].append(duplicate)
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="severity keyword levels must not overlap"):
        LoadStrategySettings(changed_path)


def test_long_factor_index_cannot_hide_a_provider_placeholder(tmp_path) -> None:
    raw = _strategy_document()
    raw["factor_index"]["value_score"]["formula"] = "provider supplied 0-100"
    changed_path = _write_strategy_config(tmp_path, raw)

    with pytest.raises(ConfigurationError, match="value_score.formula"):
        LoadStrategySettings(changed_path)
